import os
import time
import random
from pytorch_lightning.utilities.types import LRSchedulerType
from torch.optim import lr_scheduler
from tqdm import tqdm
import matplotlib.pyplot as plt
from argparse import ArgumentParser

import torch
from torch import nn
import torch.nn.functional as F
from torch import optim
from torch.utils.data import DataLoader

import torch.distributed as dist
import torch.multiprocessing as mp
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter

from data import PlanningDataset, SequencePlanningDataset, Comma2k19SequenceDataset
from model import PlaningNetwork, MultipleTrajectoryPredictionLoss, SequencePlanningNetwork
from utils import draw_trajectory_on_ax, get_val_metric


def get_hyperparameters(parser: ArgumentParser):
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--n_workers', type=int, default=8)
    parser.add_argument('--gpus', type=int, default=1)
    parser.add_argument('--epochs', type=int, default=1)

    parser.add_argument('--resume', type=str, default='')

    parser.add_argument('--M', type=int, default=3)
    parser.add_argument('--num_pts', type=int, default=20)
    parser.add_argument('--mtp_alpha', type=float, default=1.0)
    parser.add_argument('--optimizer', type=str, default='sgd')

    try:
        exp_name = os.environ["SLURM_JOB_ID"]
    except KeyError:
        exp_name = str(time.time())
    parser.add_argument('--exp_name', type=str, default=exp_name)

    return parser


def setup(rank, world_size):
    master_addr = 'localhost'
    master_port = random.randint(30000, 50000)
    os.environ['MASTER_ADDR'] = master_addr
    os.environ['MASTER_PORT'] = str(master_port)

    torch.cuda.set_device(rank)
    dist.init_process_group('nccl', rank=rank, world_size=world_size)
    print('Distributed Environment Initialized at %s:%s' % (master_addr, master_port))


def get_dataloader(rank, world_size, batch_size, pin_memory=False, num_workers=0):
    train = Comma2k19SequenceDataset('data/comma2k19_val_non_overlap.txt', 'data/comma2k19/','train', use_memcache=False)
    val = Comma2k19SequenceDataset('data/comma2k19_val_non_overlap.txt', 'data/comma2k19/','val', use_memcache=False)

    train_sampler = DistributedSampler(train, num_replicas=world_size, rank=rank, shuffle=True, drop_last=True)
    val_sampler = DistributedSampler(val, num_replicas=world_size, rank=rank, shuffle=False, drop_last=True)

    loader_args = dict(num_workers=num_workers, persistent_workers=True if num_workers > 0 else False, prefetch_factor=2, pin_memory=pin_memory)
    train_loader = DataLoader(train, batch_size, sampler=train_sampler, **loader_args)
    val_loader = DataLoader(val, batch_size, sampler=val_sampler, **loader_args)

    return train_loader, val_loader


def cleanup():
    dist.destroy_process_group()

class SequenceBaselineV1(nn.Module):
    def __init__(self, M, num_pts, mtp_alpha, lr, optimizer) -> None:
        super().__init__()
        self.M = M
        self.num_pts = num_pts
        self.mtp_alpha = mtp_alpha
        self.lr = lr
        self.optimizer = optimizer

        self.net = SequencePlanningNetwork(M, num_pts)

        self.optimize_per_n_step = 40

    @staticmethod
    def configure_optimizers(args, model):
        if args.optimizer == 'sgd':
            optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=0.01)
        elif args.optimizer == 'adam':
            optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=0.01)
        else:
            raise NotImplementedError
        lr_scheduler = optim.lr_scheduler.StepLR(optimizer, 20, 0.9)

        return optimizer, lr_scheduler

    def forward(self, x, hidden=None):
        if hidden is None:
            hidden = torch.zeros((2, x.size(0), 512)).to(self.device)
        return self.net(x, hidden)

    def training_step(self, batch, batch_idx=-1):
        pass

    def validation_step(self, batch, batch_idx):
        seq_inputs, seq_labels = batch['seq_input_img'], batch['seq_future_poses']

        bs = seq_labels.size(0)
        seq_length = seq_labels.size(1)
        
        hidden = torch.zeros((2, bs, 512)).to(self.device)
        for t in range(seq_length):
            inputs, labels = seq_inputs[:, t, :, :, :], seq_labels[:, t, :, :]
            pred_cls, pred_trajectory, hidden = self.net(inputs, hidden)

            metrics = get_val_metric(pred_cls, pred_trajectory.view(-1, self.M, self.num_pts, 3), labels)
            self.log_dict(metrics)


def main(rank, world_size, args):
    setup(rank, world_size)

    if rank == 0:
        writer = SummaryWriter()

    train_dataloader, val_dataloader = get_dataloader(rank, world_size, args.batch_size, False, args.n_workers)
    model = SequenceBaselineV1(args.M, args.num_pts, args.mtp_alpha, args.lr, args.optimizer)
    use_sync_bn = True  # TODO
    if use_sync_bn:
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = model.cuda()
    optimizer, lr_scheduler = model.configure_optimizers(args, model)
    model: SequenceBaselineV1
    if args.resume:
        model.load_state_dict(args.resume, strict=True)
    model = nn.parallel.DistributedDataParallel(model, device_ids=[rank], find_unused_parameters=True, broadcast_buffers=False)
    loss = MultipleTrajectoryPredictionLoss(args.mtp_alpha, args.M, args.num_pts, distance_type='angle')

    num_steps = 0

    for epoch in tqdm(range(args.epochs)):
        train_dataloader.sampler.set_epoch(epoch)
        
        for batch_idx, data in enumerate(tqdm(train_dataloader, leave=False)):
            seq_inputs, seq_labels = data['seq_input_img'].cuda(), data['seq_future_poses'].cuda()
            bs = seq_labels.size(0)
            seq_length = seq_labels.size(1)
            
            hidden = torch.zeros((2, bs, 512)).cuda()
            total_loss = 0
            for t in tqdm(range(seq_length), leave=False):
                num_steps += 1
                inputs, labels = seq_inputs[:, t, :, :, :], seq_labels[:, t, :, :]
                pred_cls, pred_trajectory, hidden = model(inputs, hidden)

                cls_loss, reg_loss = loss(pred_cls, pred_trajectory, labels)
                total_loss += (cls_loss + args.mtp_alpha * reg_loss.mean()) / model.module.optimize_per_n_step
            
                if rank == 0:
                    writer.add_scalar('loss/cls', cls_loss, num_steps)
                    writer.add_scalar('loss/reg', reg_loss.mean(), num_steps)
                    writer.add_scalar('loss/reg_x', reg_loss[0], num_steps)
                    writer.add_scalar('loss/reg_y', reg_loss[1], num_steps)
                    writer.add_scalar('loss/reg_z', reg_loss[2], num_steps)

                if (t + 1) % model.module.optimize_per_n_step == 0:
                    hidden = hidden.clone().detach()
                    optimizer.zero_grad()
                    total_loss.backward()
                    optimizer.step()
                    lr_scheduler.step()
                    total_loss = 0

            if not isinstance(total_loss, int):
                optimizer.zero_grad()
                total_loss.backward()
                optimizer.step()
                lr_scheduler.step()

        if (epoch + 1) % 10 == 0:
            print('skipping val...')
            continue
            for batch_idx, data in enumerate(val_dataloader):
                data = data.cuda()
                model.validation_step(data)  # TODO

    cleanup()


if __name__ == "__main__":

    parser = ArgumentParser()
    parser = get_hyperparameters(parser)
    args = parser.parse_args()

    world_size = args.gpus
    mp.spawn(
        main,
        args=(world_size, args),
        nprocs=world_size
    )
