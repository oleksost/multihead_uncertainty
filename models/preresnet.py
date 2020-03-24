import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.autograd import Variable
import sys, os
import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from utils.utils import to_one_hot, mixup_process, get_lambda
#from load_data import per_image_standardization
import random

def per_image_standardization(x):
    y = x.view(-1, x.shape[1]*x.shape[2]*x.shape[3])
    mean = y.mean(dim=1, keepdim = True).expand_as(y)
    std = y.std(dim=1, keepdim = True).expand_as(y)
    if torch.cuda.is_available():
        o = torch.cuda.FloatTensor([x.shape[1]*x.shape[2]*x.shape[3]])
    else:
        o = torch.FloatTensor([x.shape[1] * x.shape[2] * x.shape[3]])
    adjusted_std = torch.max(std, 1.0/torch.sqrt(o))
    y = (y- mean)/ adjusted_std
    standarized_input =  y.view(x.shape[0],x.shape[1],x.shape[2],x.shape[3])
    return standarized_input

class PreActBlock(nn.Module):
    '''Pre-activation version of the BasicBlock.'''
    expansion = 1

    def __init__(self, in_planes, planes, stride=1, activation=torch.nn.ReLU()):
        super(PreActBlock, self).__init__()
        self.activation = activation
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)

        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion * planes, kernel_size=1, stride=stride, bias=False)
            )

    def forward(self, x):
        out = self.activation(self.bn1(x)) #F.relu(self.bn1(x))
        shortcut = self.shortcut(out) if hasattr(self, 'shortcut') else x
        out = self.conv1(out)
        out = self.conv2(self.activation(self.bn2(out))) #F.relu(self.bn2(out)))
        out = torch.add(out,shortcut)
        return out


class PreActBlock_act_bfore_bn(nn.Module):
    '''Pre-activation version of the BasicBlock.'''
    expansion = 1

    def __init__(self, in_planes, planes, stride=1, activation=torch.nn.ReLU()):
        super(PreActBlock, self).__init__()
        self.activation = activation
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)

        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion * planes, kernel_size=1, stride=stride, bias=False)
            )

    def forward(self, x):
        out = self.bn1(self.activation(x)) #F.relu(self.bn1(x))
        shortcut = self.shortcut(out) if hasattr(self, 'shortcut') else x
        out = self.conv1(out)
        out = self.conv2(self.bn2(self.activation(out))) #self.conv2(F.relu(self.bn2(out)))
        out = torch.add(out,shortcut)
        return out


class PreActBottleneck(nn.Module):
    '''Pre-activation version of the original Bottleneck module.'''
    expansion = 4

    def __init__(self, in_planes, planes, stride=1, activation=torch.nn.ReLU()):
        super(PreActBottleneck, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.activation = activation
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, self.expansion * planes, kernel_size=1, bias=False)

        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion * planes, kernel_size=1, stride=stride, bias=False)
            )

    def forward(self, x):
        out = self.activation(self.bn1(x))
        shortcut = self.shortcut(out) if hasattr(self, 'shortcut') else x
        out = self.conv1(out)
        out = self.conv2(self.activation(self.bn2(out)))
        out = self.conv3(self.activation(self.bn3(out)))
        out = torch.add(out,shortcut)
        return out


class PreActResNet(nn.Module):
    def __init__(self, block, num_blocks, initial_channels, num_classes, per_img_std=False, stride=1, activation=torch.nn.ReLU(), heads_number=1):
        super(PreActResNet, self).__init__()
        self.in_planes = initial_channels
        self.num_classes = num_classes
        self.cur_task = 0
        self.heads_number = heads_number
        self.activation = activation
        self.per_img_std = per_img_std
        if torch.cuda.is_available():
            self.device='cuda'
        else:
            self.device='cpu'
        # import pdb; pdb.set_trace()
        self.conv1 = nn.Conv2d(3, initial_channels, kernel_size=3, stride=stride, padding=1, bias=False)
        self.layer1 = self._make_layer(block, initial_channels, num_blocks[0], stride=1, activation=self.activation)
        self.layer2 = self._make_layer(block, initial_channels * 2, num_blocks[1], stride=2, activation=self.activation)
        self.layer3 = self._make_layer(block, initial_channels * 4, num_blocks[2], stride=2, activation=self.activation)
        self.layer4 = self._make_layer(block, initial_channels * 8, num_blocks[3], stride=2, activation=self.activation)
        self.linear = nn.ModuleList()
        for t in range(heads_number):
            self.linear.append(nn.Linear(initial_channels * 8 * block.expansion, num_classes))
        #self.linear = nn.Linear(initial_channels * 8 * block.expansion, num_classes)

    def _make_layer(self, block, planes, num_blocks, stride, activation):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride, activation))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def compute_h1(self, x):
        out = x
        out = self.conv1(out)
        out = self.layer1(out)
        return out

    def compute_h2(self, x):
        out = x
        out = self.conv1(out)
        out = self.layer1(out)
        out = self.layer2(out)
        return out

    def forward(self, x, target=None, mixup=False, mixup_hidden=False, mixup_alpha=None, task=-1):
        # import pdb; pdb.set_trace()
        if self.per_img_std:
            x = per_image_standardization(x)

        if mixup_hidden:
            layer_mix = random.randint(0, 2)
        elif mixup:
            layer_mix = 0
        else:
            layer_mix = None

        out = x

        if mixup_alpha is not None:
            lam = get_lambda(mixup_alpha)
            lam = torch.from_numpy(np.array([lam]).astype('float32')).to(self.device)#cuda()
            lam = Variable(lam)

        if target is not None:
            target_reweighted = to_one_hot(target, self.num_classes)

        if layer_mix == 0:
            out, target_reweighted = mixup_process(out, target_reweighted, lam=lam)

        out = self.conv1(out)
        out = self.layer1(out)

        if layer_mix == 1:
            out, target_reweighted = mixup_process(out, target_reweighted, lam=lam)

        out = self.layer2(out)

        if layer_mix == 2:
            out, target_reweighted = mixup_process(out, target_reweighted, lam=lam)

        out = self.layer3(out)
        if layer_mix == 3:
            out, target_reweighted = mixup_process(out, target_reweighted, lam=lam)

        out = self.layer4(out)
        out = F.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)
        if task==-1:
            out = self.linear[self.cur_task](x)
        else:
            out = self.linear[task](out)


        if target is not None:
            return out, target_reweighted
        else:
            return out


def preactresnet18(num_classes=10, dropout=False, per_img_std=False, stride=1, activation=torch.nn.ReLU, heads_number=1):
    return PreActResNet(PreActBlock, [2, 2, 2, 2], 64, num_classes, per_img_std, stride=stride, activation=activation, heads_number=heads_number)


def preactresnet34(num_classes=10, dropout=False, per_img_std=False, stride=1, activation=torch.nn.ReLU, heads_number=1, act_bfore_bn=False):
    if act_bfore_bn:
        block = PreActBlock_act_bfore_bn
    else:
        block = PreActBlock
    return PreActResNet(block, [3, 4, 6, 3], 64, num_classes, per_img_std, stride=stride, activation=activation, heads_number=heads_number)


def preactresnet50(num_classes=10, dropout=False, per_img_std=False, stride=1, activation=torch.nn.ReLU, heads_number=1):
    return PreActResNet(PreActBottleneck, [3, 4, 6, 3], 64, num_classes, per_img_std, stride=stride, activation=activation, heads_number=heads_number)


def preactresnet101(num_classes=10, dropout=False, per_img_std=False, stride=1):
    return PreActResNet(PreActBottleneck, [3, 4, 23, 3], 64, num_classes, per_img_std, stride=stride)


def preactresnet152(num_classes=10, dropout=False, per_img_std=False, stride=1):
    return PreActResNet(PreActBottleneck, [3, 8, 36, 3], 64, num_classes, per_img_std, stride=stride)


def test():
    net = PreActResNet152(True, 10)
    y = net(Variable(torch.randn(1, 3, 32, 32)))
    print(y.size())


if __name__ == "__main__":
    test()
# test()