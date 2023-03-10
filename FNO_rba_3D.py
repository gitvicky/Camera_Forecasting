#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu July 13 2022


@author: vgopakum

FNO 3D on RBA Camera Data. Data Pipeline is reconstructed to be fed
in a shot-aware sequential manner in line with a Recurrent model


"""

# %%
configuration = {"Case": 'RBA Camera',
                 "Pipeline": 'Sequential',
                 "Calibration": 'Calcam',
                 "Epochs": 1,
                 "Batch Size": 4,
                 "Optimizer": 'Adam',
                 "Learning Rate": 0.005,
                 "Scheduler Step": 50,
                 "Scheduler Gamma": 0.5,
                 "Activation": 'ReLU',
                 "Normalisation Strategy": 'Min-Max',
                 "T_in": 20, 
                 "T_out": 50,
                 "Step": 10,
                 "Modes":8,
                 "Width": 16,
                 "Variables": 1,
                 "Resolution":1, 
                 "Noise":0.0}


# %% 
from simvue import Run
run = Run()
run.init(folder="/FNO_Camera", tags=['FNO', 'Camera', 'rba', 'Forecasting', '3D'], metadata=configuration)


# %%

import numpy as np
from tqdm import tqdm 
import torch
import torch.nn as nn
import torch.nn.functional as F

import matplotlib.pyplot as plt
from matplotlib import cm 

import operator
from functools import reduce
from functools import partial

import time 
from timeit import default_timer

torch.manual_seed(0)
np.random.seed(0)

import os 
path = file_loc = os.getcwd()
data_loc = os.path.dirname(os.path.dirname(os.path.dirname(os.getcwd())))
model_loc = os.getcwd()



#################################################
#
# Utilities
#
#################################################
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# normalization, pointwise gaussian
class UnitGaussianNormalizer(object):
    def __init__(self, x, eps=0.00001):
        super(UnitGaussianNormalizer, self).__init__()

        # x could be in shape of ntrain*n or ntrain*T*n or ntrain*n*T
        self.mean = torch.mean(x, 0)
        self.std = torch.std(x, 0)
        self.eps = eps

    def encode(self, x):
        x = (x - self.mean) / (self.std + self.eps)
        return x

    def decode(self, x, sample_idx=None):
        if sample_idx is None:
            std = self.std + self.eps # n
            mean = self.mean
        else:
            if len(self.mean.shape) == len(sample_idx[0].shape):
                std = self.std[sample_idx] + self.eps  # batch*n
                mean = self.mean[sample_idx]
            if len(self.mean.shape) > len(sample_idx[0].shape):
                std = self.std[:,sample_idx]+ self.eps # T*batch*n
                mean = self.mean[:,sample_idx]

        # x is in shape of batch*n or T*batch*n
        x = (x * std) + mean
        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()

# normalization, Gaussian
class GaussianNormalizer(object):
    def __init__(self, x, eps=0.00001):
        super(GaussianNormalizer, self).__init__()

        self.mean = torch.mean(x)
        self.std = torch.std(x)
        self.eps = eps

    def encode(self, x):
        x = (x - self.mean) / (self.std + self.eps)
        return x

    def decode(self, x, sample_idx=None):
        x = (x * (self.std + self.eps)) + self.mean
        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()


# normalization, scaling by range
class RangeNormalizer(object):
    def __init__(self, x, low=0.0, high=1.0):
        super(RangeNormalizer, self).__init__()
        mymin = torch.min(x, 0)[0].view(-1)
        mymax = torch.max(x, 0)[0].view(-1)

        self.a = (high - low)/(mymax - mymin)
        self.b = -self.a*mymax + high

    def encode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = self.a*x + self.b
        x = x.view(s)
        return x

    def decode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = (x - self.b)/self.a
        x = x.view(s)
        return x

    def cuda(self):
        self.a = self.a.cuda()
        self.b = self.b.cuda()

    def cpu(self):
        self.a = self.a.cpu()
        self.b = self.b.cpu()

#loss function with rel/abs Lp loss
class LpLoss(object):
    def __init__(self, d=2, p=2, size_average=True, reduction=True):
        super(LpLoss, self).__init__()

        #Dimension and Lp-norm type are postive
        assert d > 0 and p > 0

        self.d = d
        self.p = p
        self.reduction = reduction
        self.size_average = size_average

    def abs(self, x, y):
        num_examples = x.size()[0]

        #Assume uniform mesh
        h = 1.0 / (x.size()[1] - 1.0)

        all_norms = (h**(self.d/self.p))*torch.norm(x.view(num_examples,-1) - y.view(num_examples,-1), self.p, 1)

        if self.reduction:
            if self.size_average:
                return torch.mean(all_norms)
            else:
                return torch.sum(all_norms)

        return all_norms

    def rel(self, x, y):
        num_examples = x.size()[0]

        diff_norms = torch.norm(x.reshape(num_examples,-1) - y.reshape(num_examples,-1), self.p, 1)
        y_norms = torch.norm(y.reshape(num_examples,-1), self.p, 1)

        if self.reduction:
            if self.size_average:
                return torch.mean(diff_norms/y_norms)
            else:
                return torch.sum(diff_norms/y_norms)

        return diff_norms/y_norms

    def __call__(self, x, y):
        return self.rel(x, y)

# A simple feedforward neural network
class DenseNet(torch.nn.Module):
    def __init__(self, layers, nonlinearity, out_nonlinearity=None, normalize=False):
        super(DenseNet, self).__init__()

        self.n_layers = len(layers) - 1

        assert self.n_layers >= 1

        self.layers = nn.ModuleList()

        for j in range(self.n_layers):
            self.layers.append(nn.Linear(layers[j], layers[j+1]))

            if j != self.n_layers - 1:
                if normalize:
                    self.layers.append(nn.BatchNorm1d(layers[j+1]))

                self.layers.append(nonlinearity())

        if out_nonlinearity is not None:
            self.layers.append(out_nonlinearity())

    def forward(self, x):
        for _, l in enumerate(self.layers):
            x = l(x)

        return x

# %%


#Complex multiplication
def compl_mul2d(a, b):
    op = partial(torch.einsum, "bctq,dctq->bdtq")
    return torch.stack([
        op(a[..., 0], b[..., 0]) - op(a[..., 1], b[..., 1]),
        op(a[..., 1], b[..., 0]) + op(a[..., 0], b[..., 1])
    ], dim=-1)


#Adding Gaussian Noise to the training dataset
class AddGaussianNoise(object):
    def __init__(self, mean=0., std=1.):
        self.mean = torch.FloatTensor([mean])
        self.std = torch.FloatTensor([std])
        
    def __call__(self, tensor):
        return tensor + torch.randn(tensor.size()).cuda() * self.std + self.mean
    
    def __repr__(self):
        return self.__class__.__name__ + '(mean={0}, std={1})'.format(self.mean, self.std)

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()

# additive_noise = AddGaussianNoise(0.0, configuration['Noise'])
# additive_noise.cuda()

# %%

# %%

################################################################
# 3d fourier layers
################################################################
step = configuration['Step']

class SpectralConv3d(nn.Module):
    def __init__(self, in_channels, out_channels, modes1, modes2, modes3):
        super(SpectralConv3d, self).__init__()

        """
        3D Fourier layer. It does FFT, linear transform, and Inverse FFT.    
        """

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.modes1 = modes1 #Number of Fourier modes to multiply, at most floor(N/2) + 1
        self.modes2 = modes2
        self.modes3 = modes3 

        self.scale = (1 / (in_channels * out_channels))
        self.weights1 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights2 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights3 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights4 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))

    # Complex multiplication
    def compl_mul3d(self, input, weights):
        # (batch, in_channel, x,y,t ), (in_channel, out_channel, x,y,t) -> (batch, out_channel, x,y,t)
        return torch.einsum("bixyz,ioxyz->boxyz", input, weights)

    def forward(self, x):
        batchsize = x.shape[0]
        #Compute Fourier coeffcients up to factor of e^(- something constant)
        x_ft = torch.fft.rfftn(x, dim=[-3,-2,-1])

        # Multiply relevant Fourier modes
        out_ft = torch.zeros(batchsize, self.out_channels, x.size(-3), x.size(-2), x.size(-1)//2 + 1, dtype=torch.cfloat, device=x.device)
        out_ft[:, :, :self.modes1, :self.modes2, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, :self.modes1, :self.modes2, :self.modes3], self.weights1)
        out_ft[:, :, -self.modes1:, :self.modes2, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, -self.modes1:, :self.modes2, :self.modes3], self.weights2)
        out_ft[:, :, :self.modes1, -self.modes2:, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, :self.modes1, -self.modes2:, :self.modes3], self.weights3)
        out_ft[:, :, -self.modes1:, -self.modes2:, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, -self.modes1:, -self.modes2:, :self.modes3], self.weights4)

        #Return to physical space
        x = torch.fft.irfftn(out_ft, s=(x.size(-3), x.size(-2), x.size(-1)))
        return x


class FNO3d(nn.Module):
    def __init__(self, modes1, modes2, modes3, width):
        super(FNO3d, self).__init__()

        """
        The overall network. It contains 4 layers of the Fourier layer.
        1. Lift the input to the desire channel dimension by self.fc0 .
        2. 4 layers of the integral operators u' = (W + K)(u).
            W defined by self.w; K defined by self.conv .
        3. Project from the channel space to the output space by self.fc1 and self.fc2 .
        
        input: the solution of the previous T_in timesteps + 3 locations (u(t-T_in, x, y), ..., u(t-1, x, y),  x, y)
        input shape: (batchsize, x=x_discretistion, y=y_discretisation, c=T_in)
        output: the solution of the next timestep
        output shape: (batchsize, x=x_discretisation, y=y_discretisatiob, c=step)
        """

        self.modes1 = modes1
        self.modes2 = modes2
        self.modes3 = modes3
        self.width = width
        self.fc0 = nn.Linear(T_in+3, self.width)

        self.conv0 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv1 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv2 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv3 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.w0 = nn.Conv3d(self.width, self.width, 1)
        self.w1 = nn.Conv3d(self.width, self.width, 1)
        self.w2 = nn.Conv3d(self.width, self.width, 1)
        self.w3 = nn.Conv3d(self.width, self.width, 1)
        # self.bn0 = torch.nn.BatchNorm3d(self.width)
        # self.bn1 = torch.nn.BatchNorm3d(self.width)
        # self.bn2 = torch.nn.BatchNorm3d(self.width)
        # self.bn3 = torch.nn.BatchNorm3d(self.width)

        self.fc1 = nn.Linear(self.width, self.width)
        self.fc2 = nn.Linear(self.width, step)

    def forward(self, x):
        grid = self.get_grid(x.shape, x.device)
        x = torch.cat((x, grid), dim=-1)
        print(x.shape)
        x = x.permute(0, 1, 2, 4, 3)
        print(x.shape)
        x = self.fc0(x)
        x = x.permute(0, 4, 1, 2, 3)

        x1 = self.conv0(x)
        x2 = self.w0(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv1(x)
        x2 = self.w1(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv2(x)
        x2 = self.w2(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv3(x)
        x2 = self.w3(x)
        x = x1 + x2
        
        x = x.permute(0, 2, 3, 4, 1) # pad the domain if input is non-periodic
        x = self.fc1(x)
        x = F.gelu(x)
        x = x.permute(0, 4, 1, 2, 3)
        x = self.fc2(x)
        x = x.permute(0, 2, 3, 1, 4)
        
        #Averaging T_out
        # x = torch.mean(x, 3)
        #Possibly worth exploring averaging out the T_out index
        return x

#Using x and y values from the simulation discretisation 
    def get_grid(self, shape, device):
        batchsize, size_x, size_y = shape[0], shape[1], shape[2]
        gridx = torch.tensor(x_grid, dtype=torch.float)
        gridx = gridx.reshape(1, size_x, 1, 1, 1).repeat([batchsize, 1, size_y, , 1])        gridy = torch.tensor(y_grid, dtype=torch.float)
        gridy = gridy.reshape(1, 1, size_y, 1).repeat([batchsize, size_x, 1, 1])
        return torch.cat((gridx, gridy), dim=-1).to(device)

## Arbitrary grid discretisation 
    # def get_grid(self, shape, device):
    #     batchsize, size_x, size_y = shape[0], shape[1], shape[2]
    #     gridx = torch.tensor(np.linspace(0, 1, size_x), dtype=torch.float)
    #     gridx = gridx.reshape(1, size_x, 1, 1).repeat([batchsize, 1, size_y, 1])
    #     gridy = torch.tensor(np.linspace(0, 1, size_y), dtype=torch.float)
    #     gridy = gridy.reshape(1, 1, size_y, 1).repeat([batchsize, size_x, 1, 1])
    #     return torch.cat((gridx, gridy), dim=-1).to(device)


    def count_params(self):
        c = 0
        for p in self.parameters():
            c += reduce(operator.mul, list(p.size()))

        return c
# %%
model = FNO3d(16, 16, 8, 32)
model(torch.empty(10, 40, 400, 512, 23))

# %%


################################################################
# Loading Data 
################################################################

# %%

data =  np.load(data_loc + '/Data/Cam_Data/Cleaned_Data/rba_30280_30360.npy')
data_2 = np.load(data_loc + '/Data/Cam_Data/rba_fno_data_2.npy')
# data =  np.load(data_loc + '/Data/Cam_Data/rba_data_608x768.npy')
data_calib =  np.load(data_loc + '/Data/Cam_Data/Cleaned_Data/Calibrations/rba_rz_pos_30280_30360.npz')

res = configuration['Resolution']
gridx = data_calib['r_pos'][::res, ::res]
gridy = data_calib['z_pos'][::res, ::res]
u_sol = data.astype(np.float32)[:,:,::res, ::res]

u_2_sol = data_2.astype(np.float32)[:,:,::res,::res]
u_sol = np.vstack((u_sol, u_2_sol))

np.random.shuffle(u_sol)

# %%

grid_size_x = u_sol.shape[2]
grid_size_y = u_sol.shape[3]

u = torch.from_numpy(u_sol)
u = u.permute(0, 2, 3, 1)


ntrain = 75
ntest = 11
batch_size_test = ntest 


S_x = grid_size_x #Grid Size
S_y = grid_size_y #Grid Size

modes = configuration['Modes']
width = configuration['Width']
output_size = configuration['Step']

batch_size = configuration['Batch Size']
batch_size2 = batch_size


t1 = default_timer()


T_in = input_size = configuration['T_in']
T = configuration['T_out']
T_out = T
step = output_size = configuration['Step']

# %%
################################################################
# Sort Data into test/train sets -- Sequential
################################################################


t_sets = T_in + T_out - input_size - output_size

u1 = torch.zeros(len(u), t_sets, S_x, S_y, input_size)
u2 = torch.zeros(len(u), t_sets, S_x, S_y, step)

for ii in tqdm(range(len(u))):
    for jj in range(t_sets):
        u1[ii, jj] = u[ii, :, :, jj:jj+input_size]
        u2[ii, jj] = u[ii, :, :, jj+input_size:jj+input_size+step]


modes = configuration['Modes']
width = configuration['Width']

batch_size = configuration['Batch Size']
batch_size2 = batch_size


t1 = default_timer()

train_a = u1[:ntrain]
train_u = u2[:ntrain]

test_a = u1[-ntest:]
test_u = u2[-ntest:]

print(train_u.shape)
print(test_u.shape)

# %%
# a_normalizer = UnitGaussianNormalizer(train_a)
a_normalizer = RangeNormalizer(train_a)
train_a = a_normalizer.encode(train_a)
test_a = a_normalizer.encode(test_a)

# y_normalizer = UnitGaussianNormalizer(train_u)
y_normalizer = RangeNormalizer(train_u)
train_u = y_normalizer.encode(train_u)
test_u_norm = y_normalizer.encode(test_u)

# %%

#Using arbitrary R and Z positions sampled uniformly within a specified domain range. 
x_grid = np.linspace(-1.0, -2.0, 400)[::res]
# x = np.linspace(-1.0, -2.0, 608)[::res]
gridx = torch.tensor(x_grid, dtype=torch.float)
gridx = gridx.reshape(1, S_x, 1, 1).repeat([1, 1, S_y, 1])

y_grid = np.linspace(0.0, 1.0, 512)[::res]
# y = np.linspace(-1.0, 0.0, 768)[::res]
gridy = torch.tensor(y_grid, dtype=torch.float)
gridy = gridy.reshape(1, 1, S_y, 1).repeat([1, S_x, 1, 1])

#Using the calibrated R and Z positions averaged over the time and shots. 
gridx = torch.tensor(gridx, dtype=torch.float)
gridy = torch.tensor(gridy, dtype=torch.float)
gridx = gridx.reshape(1, S_x, S_y, 1)
gridy = gridy.reshape(1, S_x, S_y, 1)

# train_a = torch.cat((train_a, gridx.repeat([ntrain,1,1,1]), gridy.repeat([ntrain,1,1,1])), dim=-1)
# test_a = torch.cat((test_a, gridx.repeat([ntest,1,1,1]), gridy.repeat([ntest,1,1,1])), dim=-1)

gridx = gridx.to(device)
gridy = gridy.to(device)

train_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(train_a, train_u), batch_size=batch_size, shuffle=True)
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u_norm), batch_size=batch_size_test, shuffle=False)

t2 = default_timer()
print('preprocessing finished, time used:', t2-t1)

# %%

################################################################
# training and evaluation
################################################################

model = FNO3d(modes+8,modes+8,modes, width)
run.update_metadata({'Number of Params': int(model.count_params())})
print("Number of model params : " + str(model.count_params()))

# model = nn.DataParallel(model, device_ids = [0,1])
model.to(device)

# wandb.watch(model, log='all')

optimizer = torch.optim.Adam(model.parameters(), lr=configuration['Learning Rate'], weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=configuration['Scheduler Step'], gamma=configuration['Scheduler Gamma'])

myloss = nn.MSELoss()
    
gridx = gridx.to(device)
gridy = gridy.to(device)
# gridt = gridt.to(device)

# %%
epochs = configuration['Epochs']
if torch.cuda.is_available():
    y_normalizer.cuda()

# %%
#Sequential 

start_time = time.time()
for ep in tqdm(range(epochs)):
    model.train()
    train_l2 = 0 
    test_l2 = 0
    t1 = default_timer()
    for xx, yy in train_loader:
        optimizer.zero_grad()
        
        xx = xx.to(device)
        yy = yy.to(device)
        
        out = model(xx)
        
        loss = myloss(out, yy)
        train_l2 += loss

        loss.backward()
        optimizer.step()

    with torch.no_grad():
        for xx, yy in test_loader:
            loss = 0
            xx = xx.to(device)
            yy = yy.to(device)

            pred = model(xx)
            loss = myloss(pred, yy)
            test_l2 += loss.item()


    t2 = default_timer()
    scheduler.step()
    train_loss = train_l2 / ntrain
    test_loss = test_l2 / ntest
    
    print('Epochs: %d, Time: %.2f, Train Loss: %.3e, Test Loss: %.3e' % (ep, t2 - t1, train_loss, test_loss))
    
    run.log_metrics({'Train Loss': train_loss, 
            'Test Loss': test_loss})
    
train_time = time.time() - start_time 

# %%

model_loc = file_loc + '/Models/FNO_rba_' + run.name + '.pth'
torch.save(model.state_dict(),  model_loc)
        

            
# %%
#Testing 
#Sequential

test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u), batch_size=1, shuffle=False)

pred_set = torch.zeros(test_u.shape)
index = 0

with torch.no_grad():
    for xx, yy in tqdm(test_loader):
        
        xx = xx.to(device)
        yy = yy.to(device)
        
        pred = model(xx)
        pred_set[index]=pred
        index += 1
    
test_l2 = (pred_set - test_u_norm).pow(2).mean()
print('Testing Error: %.3e' % (test_l2))



run.update_metadata({'Training Time': float(train_time),
                     'MSE Test Error': float(test_l2)
                    })

pred_set = y_normalizer.decode(pred_set.to(device)).cpu()

# %%
idx = np.random.randint(0, ntest) 
idx = 6 

u_field = test_u[idx].cpu().detach().numpy()

fig = plt.figure(figsize=plt.figaspect(0.5))
ax = fig.add_subplot(2,3,1)
ax.imshow(u_field[:,:,0], cmap=cm.coolwarm)
ax.title.set_text('Initial')
ax.set_ylabel('Camera')

ax = fig.add_subplot(2,3,2)
ax.imshow(u_field[:,:,int(T_out/2)], cmap=cm.coolwarm)
ax.title.set_text('Middle')

ax = fig.add_subplot(2,3,3)
ax.imshow(u_field[:,:,-1], cmap=cm.coolwarm)
ax.title.set_text('Final')


u_field = pred_set[idx].cpu().detach().numpy()

ax = fig.add_subplot(2,3,4)
ax.imshow(u_field[:,:,0], cmap=cm.coolwarm)
ax.set_ylabel('FNO')

ax = fig.add_subplot(2,3,5)
ax.imshow(u_field[:,:,int(T_out/2)], cmap=cm.coolwarm)

ax = fig.add_subplot(2,3,6)
ax.imshow(u_field[:,:,-1], cmap=cm.coolwarm)

output_plot = file_loc + '/Plots/rba3D_' + run.name + '.png'
plt.savefig(output_plot)

# %% 

CODE = ['FNO_rba_3D.py']
INPUTS = []
OUTPUTS = [model_loc, output_plot]

# Save code files
for code_file in CODE:
    if os.path.isfile(code_file):
        run.save(code_file, 'code')
    elif os.path.isdir(code_file):
        run.save_directory(code_file, 'code', 'text/plain', preserve_path=True)
    else:
        print('ERROR: code file %s does not exist' % code_file)


# Save input files
for input_file in INPUTS:
    if os.path.isfile(input_file):
        run.save(input_file, 'input')
    elif os.path.isdir(input_file):
        run.save_directory(input_file, 'input', 'text/plain', preserve_path=True)
    else:
        print('ERROR: input file %s does not exist' % input_file)


# Save output files
for output_file in OUTPUTS:
    if os.path.isfile(output_file):
        run.save(output_file, 'output')
    elif os.path.isdir(output_file):
        run.save_directory(output_file, 'output', 'text/plain', preserve_path=True)   
    else:
        print('ERROR: output file %s does not exist' % output_file)


# %%