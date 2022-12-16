import hydra
import pytorch_lightning as pl
import argparse

import torch
from omegaconf import OmegaConf
from path import Path
from tqdm import tqdm
import sys
sys.path.append('/scratch/pl2285/ddpm/DDPMs-Pytorch')

from ddpm_pytorch.model.classifier_free_ddpm import GaussianDDPMClassifierFreeGuidance
import torchvision

from ddpm_pytorch.utils.paths import SCHEDULER

import numpy as np
import random
from torchmetrics.image.inception import InceptionScore
from torchmetrics.image.fid import FrechetInceptionDistance

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import datasets, transforms

scheduler_paths = [p for p in SCHEDULER.files('*.yaml')]
scheduler_names = [x.basename().replace('.yaml', '') for x in scheduler_paths]
scheduler_map = {name: path for name, path in zip(scheduler_names, scheduler_paths)}

resize = transforms.Resize(28)

class ConvNet(nn.Module):
    def __init__(self):
        super(ConvNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=(5, 5), padding = 2, bias = False)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=(3, 3), padding = 1)
        self.conv3 = nn.Conv2d(64, 128, kernel_size=(3, 3), padding = 1)
        self.conv4 = nn.Conv2d(128, 256, kernel_size=(3, 3), padding = 1)
        self.maxpool = nn.MaxPool2d(kernel_size=(2, 2), stride = 2)
        self.linear1 = nn.Linear(49 * 256, 64)
        self.linear2 = nn.Linear(64, 10)

    def forward(self, input):
        input = torch.relu(self.conv1(input))
        input = torch.relu(self.conv2(input))
        input = self.maxpool(input)
        input = torch.relu(self.conv3(input))
        input = torch.relu(self.conv4(input))
        input = self.maxpool(input)
        input = input.view(-1, 49 * 256)
        input = torch.relu(self.linear1(input))
        output = self.linear2(input)
        return output


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--run', type=Path, required=True, help='Path to the checkpoint file')
    parser.add_argument('--seed', '-s', type=int, default=0, help='Random seed')
    parser.add_argument('--device', '-d', type=str, default='cpu', help='Device to use')
    parser.add_argument('--batch-size', '-b', type=int, default=20, help='Batch size')
    parser.add_argument('-w', type=float, default=0.3, help='Class guidance')
    parser.add_argument('--scheduler', choices=scheduler_names, default=None,
                        help='use a custom scheduler', dest='scheduler')
    parser.add_argument('-T', type=int, default=None, help='Number of diffusion steps')
    return parser.parse_args()


@torch.no_grad()
def main():
    """
    Generate images from a trained model in the checkpoint folder
    """
    args = parse_args()

    network = ConvNet()
    network.load_state_dict(torch.load('/scratch/pl2285/ddpm/DDPMs-Pytorch/ddpm_pytorch/model.pth'))
    network.eval()
    run_path = args.run.abspath()
    pl.seed_everything(args.seed)
    assert run_path.exists(), run_path
    assert run_path.basename().endswith('.ckpt'), run_path
    print('loading model from', run_path)
    hparams = OmegaConf.load(run_path.parent / 'config.yaml')
    if args.T is not None:
        hparams.T = args.T
    if args.w is None:
        args.w = hparams.model.w
    model_hparams = hparams.model
    denoiser = hydra.utils.instantiate(model_hparams.denoiser_module)
    if args.scheduler is None:
        scheduler = hydra.utils.instantiate(hparams.scheduler)
    else:
        scheduler_conf = OmegaConf.load(scheduler_map[args.scheduler])
        scheduler_conf.T = hparams.noise_steps
        scheduler = hydra.utils.instantiate(scheduler_conf)
    model = GaussianDDPMClassifierFreeGuidance(
        denoiser_module=denoiser, T=model_hparams.T,
        w=args.w, p_uncond=model_hparams.p_uncond, width=model_hparams.width,
        height=model_hparams.height, input_channels=model_hparams.input_channels,
        num_classes=model_hparams.num_classes, logging_freq=1000, v=model_hparams.v,
        variance_scheduler=scheduler).to(args.device)
    model.load_state_dict(torch.load(run_path, map_location=args.device)['state_dict'])
    model = model.eval()
    images = []
    xs = []
    score_sum = 0
    model.on_fit_start()

    
    data_transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])
    for i_c in tqdm(range(model.num_classes)):
        c = torch.zeros((args.batch_size, model.num_classes), device=args.device)
        c[:, i_c] = 1
        gen_images = model.generate(batch_size=args.batch_size, c=c)
        plotset = datasets.MNIST(root = './dataset',train=False, download=True, transform=data_transform)
        x = np.array(plotset.targets)
        indices = np.where(x == i_c)
        indices = indices[0]
        samples = indices[random.sample(range(len(indices)), args.batch_size)]
        x = plotset.data[samples]
        x = x.unsqueeze(1)
        xs.append(x)
        
        images.append(gen_images.cpu())
        pred = F.log_softmax(network(gen_images.cpu()))
        score = torch.mean(pred[:, i_c])
        score_sum -= score
    images = torch.cat(images, dim=0)
    torchvision.utils.save_image(images, run_path.parent / 'generated_images.png', nrow=20, padding=2, normalize=True)
    xs = torch.cat(xs, dim = 0)
    inception = InceptionScore(feature = network, normalize = True)
    inception.update(images)
    fid = FrechetInceptionDistance(feature = 64, normalize = True)
    xs = xs.expand(xs.shape[0], 3, xs.shape[2], xs.shape[3])
    images = images.expand(images.shape[0], 3, images.shape[2], images.shape[3])
    fid.update(xs, real=True)
    images = images.type(torch.uint8)
    fid.update(images, real=False)        
    print("IS: " + str(inception.compute()))
    print("FID: " + str(fid.compute()))
    print("NLL: " + str(score_sum / model.num_classes))


if __name__ == '__main__':
    main()
