from itertools import islice
import torch
import torch.nn as nn
import einops
import safetensors as st
from streamvc.model import StreamVC
from streamvc.train.discriminator import Discriminator
from streamvc.train.loss import GeneratorLoss, DiscriminatorLoss, FeatureLoss, ReconstructionLoss
from streamvc.train.encoder_classifier import EncoderClassifier
from streamvc.train.libritts import get_libritts_dataloader
from accelerate import Accelerator, DataLoaderConfiguration
from accelerate.utils import ProjectConfiguration
import time
import os
import argparse

accelerator = Accelerator(log_with="tensorboard",
                          project_config=ProjectConfiguration(
                              project_dir=os.getcwd(),
                              logging_dir=os.path.join(os.getcwd(), "logs")),
                          dataloader_config=DataLoaderConfiguration(split_batches=True))
# TODO: shouldn't we set it to True only if the input size is constant? - it errors without it
torch.backends.cudnn.benchmark = True
# TODO: isn't it enabled by default? we probably don't
# torch.backends.cudnn.enabled = True

NUM_CLASSES = 100
EMBEDDING_DIMS = 64
SAMPLES_PER_FRAME = 320
TRAIN_SPLIT = "train.other.500"
DEV_SPLIT = "dev.clean"
TEST_SPLIT = "test.clean"
DEVICE = accelerator.device


def print_time(s):
    t = time.localtime()
    current_time = time.strftime("%H:%M:%S", t)
    accelerator.print(f"[{current_time}] - {s}", flush=True)


def sizeof_fmt(num, suffix="B"):
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}{suffix}"
        num /= 1024.0


def print_cuda_memory(s):
    if accelerator.device.type != "cuda":
        print_time(s)
        return
    free, total = torch.cuda.mem_get_info()
    curr = torch.cuda.memory_allocated()
    peak = torch.cuda.max_memory_allocated()

    size = {
        "allocated": curr,
        "total": total,
        "free": free,
        "peak": peak
    }

    print_time(
        " | ".join(
            map(lambda x: f"{x[0]} {sizeof_fmt(x[1]):8}", size.items()))
        + f" - {s}")


def get_optimizer(name, **args):
    if name == "Adam":
        return torch.optim.Adam(**args)
    if name == "AdamW":
        return torch.optim.AdamW(**args)


@torch.no_grad()
def get_batch_labels(hubert_model: nn.Module, batch: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    Get hubert output labels for a given audio samples batch.

    :param hubert_model: Hubert model with discrete output labels.
    :param batch: A batch of audio samples.
    :return: The output predictions generated by the Hubert model for the input batch.
    """
    labels = []
    frame_mask = mask.unfold(
        dimension=-1, size=SAMPLES_PER_FRAME, step=SAMPLES_PER_FRAME).all(dim=-1)
    for sample in batch:
        single_sample_batch = einops.rearrange(sample, 's -> 1 1 s')
        labels.append(hubert_model.units(single_sample_batch))
    labels = torch.stack(labels, dim=0)
    assert labels.shape == frame_mask.shape
    labels[~frame_mask] = -1
    return labels


@accelerator.on_main_process
def log_gradients(model, step):
    summary_writer = accelerator.get_tracker("tensorboard").tracker
    for name, param in model.named_parameters():
        if param.grad is not None:
            summary_writer.add_histogram(
                f"gradients/{name}", param.grad, global_step=step)


@accelerator.on_main_process
def log_labels(outputs_flat, labels_flat, step):
    _, predicted = torch.max(outputs_flat.data, 1)
    summary_writer = accelerator.get_tracker("tensorboard").tracker
    summary_writer.add_histogram(
        "labels/content_encoder", predicted, global_step=step)
    summary_writer.add_histogram(
        "labels/hubert", labels_flat, global_step=step)


def train_content_encoder(content_encoder: nn.Module, hubert_model: nn.Module, args: argparse.Namespace) -> nn.Module:
    """
    Train a content encoder as a classifier to predict the same labels as a discrete hubert model.

    :param content_encoder: A content encoder wrapped with a linear layer to
    :param hubert_model: Hubert model with discrete output labels.
    :param lr: Learning rate.
    :param num_epochs: Number of epochs.
    :return: The trained content encoder wrapped with a linear layer for classification.
    """
    # TODO: add epochs or number of steps when we know how much time it takes to train the model.
    wrapped_content_encoder = EncoderClassifier(
        content_encoder, EMBEDDING_DIMS, NUM_CLASSES, dropout=args.encoder_dropout).train()
    criterion = nn.CrossEntropyLoss(ignore_index=-1)
    optimizer = get_optimizer(
        args.optimizer,
        params=wrapped_content_encoder.parameters(),
        lr=args.lr,
        betas=args.betas,
        weight_decay=args.weight_decay
    )
    schedualer = torch.optim.lr_scheduler.StepLR(
        optimizer, step_size=args.schedualer_step, gamma=args.schedualer_gamma)
    dataloader = get_libritts_dataloader(
        TRAIN_SPLIT, args.batch_size, limit_samples=args.limit_batch_samples)

    [
        wrapped_content_encoder,
        optimizer,
        dataloader,
        criterion,
        schedualer
    ] = accelerator.prepare(
        wrapped_content_encoder,
        optimizer,
        dataloader,
        criterion,
        schedualer
    )

    # TODO: distributed inference with the hubert model
    hubert_model.to(accelerator.device)
    costs = []
    for epoch in range(0, args.num_epochs):
        print_time(f"epoch num: {epoch}")
        for step, (batch, mask) in enumerate(islice(dataloader, args.limit_num_batches)):
            global_step = epoch * step + step
            labels = get_batch_labels(hubert_model, batch, mask)
            with accelerator.accumulate(wrapped_content_encoder):
                outputs = wrapped_content_encoder(batch)
                outputs_flat = outputs.view(-1, NUM_CLASSES)
                labels_flat = labels.view(-1)
                loss = criterion(outputs_flat, labels_flat)
                accelerator.backward(loss)

                if args.log_gradient_interval and (step + 1) % args.log_gradient_interval == 0:
                    log_gradients(wrapped_content_encoder, global_step)

                optimizer.step()
                optimizer.zero_grad()
                schedualer.step(global_step)
                accelerator.log(
                    {
                        "loss/content_encoder": loss.item(),
                        "lr/content_encoder": schedualer.get_last_lr()[0],
                        "allocated_memory": torch.cuda.max_memory_allocated()
                        if accelerator.device.type == "cuda"
                        else 0
                    },
                    step=global_step)
                costs.append(loss.item())

            # print loss
            if (step + 1) % args.log_interval == 0:
                print_time(
                    f'[{epoch}, {step:5}] loss: {torch.tensor(costs).mean().item():.4}')
                costs = []

            if args.log_labels_interval and (step + 1) % args.log_labels_interval == 0:
                log_labels(outputs_flat, labels_flat, global_step)

            # save model checkpoints
            if (step + 1) % args.model_checkpoint_interval == 0:
                accelerator.save_model(
                    wrapped_content_encoder,
                    save_directory=os.path.join(
                        args.checkpoint_path,
                        f"{args.run_name}_content_encoder_{epoch}_{step}"
                    ))

            # compute accuracy on main process
            if (step + 1) % args.accuracy_interval == 0:
                if accelerator.is_main_process:
                    accuracy = compute_content_encoder_accuracy(
                        wrapped_content_encoder, hubert_model, dev=True)
                    accelerator.log(
                        {
                            "accuracy/content_encoder": accuracy
                        },
                        step=global_step)
                    print_time(f"accuracy: {accuracy:.2f}%")
            if accelerator.device.type == "cuda":
                torch.cuda.reset_peak_memory_stats()

    return wrapped_content_encoder


@torch.no_grad()
def compute_content_encoder_accuracy(wrapped_content_encoder: nn.Module, hubert_model: nn.Module, dev=False):
    correct = 0
    total = 0
    if dev:
        dataloader = islice(get_libritts_dataloader(DEV_SPLIT, 16), 100)
    else:
        dataloader = get_libritts_dataloader(TEST_SPLIT, 16)
    wrapped_content_encoder.to(accelerator.device).eval()
    for (batch, mask) in dataloader:
        batch = batch.to(accelerator.device)
        labels = get_batch_labels(hubert_model, batch, mask)
        outputs = wrapped_content_encoder(batch)
        outputs_flat = outputs.view(-1, NUM_CLASSES)
        labels_flat = labels.view(-1)
        _, predicted = torch.max(outputs_flat.data, 1)
        total += torch.sum(labels_flat != -1).item()
        correct += (predicted == labels_flat).sum().item()
    wrapped_content_encoder.to(accelerator.device).train()

    return 100 * correct / total


def train_streamvc(streamvc_model: StreamVC, args: argparse.Namespace) -> None:
    """
       Trains a StreamVC model.

       :param streamvc_model: The model to train.
       :param args: Hyperparameters for training.
       """
    #######################
    # Load PyTorch Models #
    #######################
    generator = streamvc_model
    discriminator = Discriminator(
        gradient_checkpointing=args.gradient_checkpointing)

    for param in generator.content_encoder.parameters():
        param.requires_grad = False

    #####################
    # Create optimizers #
    #####################
    optimizer_generator = get_optimizer(
        args.optimizer,
        params=[param for param in generator.parameters()
                if param.requires_grad],
        lr=args.lr,
        betas=args.betas,
        weight_decay=args.weight_decay)
    optimizer_discriminator = get_optimizer(
        args.optimizer,
        params=discriminator.parameters(),
        lr=args.lr,
        betas=args.betas,
        weight_decay=args.weight_decay)

    dataloader = get_libritts_dataloader(
        TRAIN_SPLIT, args.batch_size, limit_samples=args.limit_batch_samples)

    generator_loss_fn = GeneratorLoss()
    discriminator_loss_fn = DiscriminatorLoss()
    feature_loss_fn = FeatureLoss()
    reconstruction_loss_fn = ReconstructionLoss(
        gradient_checkpointing=args.gradient_checkpointing)

    [
        generator,
        discriminator,
        optimizer_generator,
        optimizer_discriminator,
        dataloader,
        generator_loss_fn,
        discriminator_loss_fn,
        feature_loss_fn,
        reconstruction_loss_fn
    ] = accelerator.prepare(
        generator,
        discriminator,
        optimizer_generator,
        optimizer_discriminator,
        dataloader,
        generator_loss_fn,
        discriminator_loss_fn,
        feature_loss_fn,
        reconstruction_loss_fn
    )

    costs = []
    for epoch in range(0, args.num_epochs):
        print_time(f"epoch num: {epoch}")
        for step, (batch, mask) in enumerate(islice(dataloader, args.limit_num_batches)):
            with accelerator.accumulate(generator, discriminator):
                x_pred_t = generator(batch, batch)
                # Remove the first 2 frames from the generated audio
                # because we match a output frame t with input frame t-2.
                x_pred_t = x_pred_t[..., SAMPLES_PER_FRAME * 2:]
                batch = batch[..., :x_pred_t.shape[-1]]

                #######################
                # Train Discriminator #
                #######################

                discriminator_fake_detached = discriminator(x_pred_t.detach())
                discriminator_real = discriminator(batch)

                discriminator_loss = discriminator_loss_fn(
                    discriminator_real, discriminator_fake_detached)

                ###################
                # Train Generator #
                ###################
                discriminator_fake = discriminator(x_pred_t)

                # Compute adversarial loss.
                adversarial_loss = generator_loss_fn(discriminator_fake)

                # Compute feature loss.
                feature_loss = feature_loss_fn(
                    discriminator_real, discriminator_fake)

                # Compute reconstruction loss.
                reconstruction_loss = reconstruction_loss_fn(batch, x_pred_t)

                generator.zero_grad()
                discriminator.zero_grad()
                losses = (
                    discriminator_loss +
                    args.lambda_adversarial * adversarial_loss +
                    args.lambda_feature * feature_loss +
                    args.lambda_reconstruction * reconstruction_loss)
                accelerator.backward(losses)
                optimizer_discriminator.step()
                optimizer_generator.step()

            ######################
            # Update tensorboard #
            ######################
            costs.append([
                discriminator_loss.item(),
                adversarial_loss.item(),
                feature_loss.item(),
                reconstruction_loss.item()
            ])

            accelerator.log(
                {
                    "loss/discriminator": discriminator_loss.item(),
                    "loss/adversarial": adversarial_loss.item(),
                    "loss/feature_matching": feature_loss.item(),
                    "loss/reconstruction": reconstruction_loss.item(),
                    "allocated_memory": torch.cuda.max_memory_allocated()
                    if accelerator.device.type == "cuda"
                    else 0
                },
                step=epoch * step + step)

            if (step + 1) % args.log_interval == 0:
                print_time(
                    f'[{epoch}, {step:5}] loss: {torch.tensor(costs).mean().item():.4}')
                costs = []
            if (step + 1) % args.model_checkpoint_interval == 0:
                accelerator.save_model(
                    generator,
                    save_directory=os.path.join(
                        args.checkpoint_path,
                        f"{args.run_name}_generator_{epoch}_{step}"
                    ))
                accelerator.save_model(
                    discriminator,
                    save_directory=os.path.join(
                        args.checkpoint_path,
                        f"{args.run_name}_discriminator_{epoch}_{step}"
                    ))
            if accelerator.device.type == "cuda":
                torch.cuda.reset_peak_memory_stats()


def main(args):
    """Main function for training StreamVC model."""
    print_time(
        f"DEVICE={accelerator.device} " +
        f"mixed_precision={accelerator.mixed_precision} " +
        f"checkpoints={args.checkpoint_path}")
    hps = {
        "batch_size": args.batch_size,
        "num_epochs": args.num_epochs,
        "lr": args.lr,
        "beta0": args.betas[0],
        "beta1": args.betas[1],
        "weight_decay": args.weight_decay,
        "gradient_accumulation_steps": accelerator.gradient_accumulation_steps,
        "optimizer": args.optimizer,
        "schedualer_step": args.schedualer_step,
        "schedualer_gamma": args.schedualer_gamma,
        "encoder-dropout": args.encoder_dropout,
    }
    print_time(f"{hps=}")
    accelerator.init_trackers(args.run_name, config=hps)
    streamvc = StreamVC(
        gradient_checkpointing=args.gradient_checkpointing)
    if args.module_to_train in ["content-encoder", "all"]:
        content_encoder = streamvc.content_encoder
        hubert_model = torch.hub.load("bshall/hubert:main", "hubert_discrete",
                                      trust_repo=True).to(torch.float32).eval()
        wrapped_content_encoder = train_content_encoder(
            content_encoder, hubert_model, args)
        accuracy = compute_content_encoder_accuracy(
            wrapped_content_encoder, hubert_model, args)
        print_time(f"{accuracy=}")
    else:
        checkpoint = st.safe_open(args.content_encoder_checkpoint, "pt")
        encoder_state_dict = {
            key[len("encoder."):]: checkpoint.get_tensor(key)
            for key in checkpoint.keys()
            if key.startswith("encoder.")
        }
        streamvc.content_encoder.load_state_dict(encoder_state_dict)

    if args.module_to_train in ["decoder-and-speaker", "all"]:
        train_streamvc(streamvc, args)

    accelerator.end_training()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", type=str, default="streamvc")
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--limit-num-batches", type=int, default=None)
    parser.add_argument("--limit-batch-samples", type=int, default=16_000 * 20)
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--checkpoint-path", type=str,
                        default=os.path.join(
                            os.environ.get("HF_HOME", os.getcwd()),
                            "checkpoints"))
    parser.add_argument("--betas", type=float, nargs=2, default=(0.5, 0.9))
    parser.add_argument("--weight-decay", type=float, default=1e-2)
    parser.add_argument("--no-gradient-checkpointing",
                        action="store_false", dest='gradient_checkpointing',
                        default=True)
    parser.add_argument("--log-interval", type=int, default=20)
    parser.add_argument("--model-checkpoint-interval", type=int, default=100)
    parser.add_argument("--lambda-feature", type=float, default=100)
    parser.add_argument("--lambda-reconstruction", type=float, default=1)
    parser.add_argument("--lambda-adversarial", type=float, default=1)
    parser.add_argument("--content-encoder-checkpoint", type=str, default="")
    parser.add_argument("--module-to-train", type=str,
                        choices=["content-encoder",
                                 "decoder-and-speaker", "all"],
                        required=True)
    parser.add_argument("--accuracy-interval", type=int, default=100)
    parser.add_argument("--optimizer", type=str,
                        default="AdamW", choices=["Adam", "AdamW"])
    parser.add_argument("--schedualer-step", type=int, default=100)
    parser.add_argument("--schedualer-gamma", type=float, default=0.1)
    parser.add_argument("--log-gradient-interval", type=int, default=None)
    parser.add_argument("--log-labels-interval", type=int, default=None)
    parser.add_argument("--encoder-dropout", type=float, default=0.1)

    args = parser.parse_args()

    if args.module_to_train == "decoder-and-speaker":
        assert args.content_encoder_checkpoint, "content-encoder-checkpoint is required for decoder training"

    main(args)
