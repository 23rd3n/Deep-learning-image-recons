'''
    File name: radon.py
    Author: Hannah Dröge
    Date created: 4/22/2021
    Python Version: 3.6
'''
import torch
import numpy as np
import matplotlib.pyplot as plt


def rampfilter(size):
    n = np.concatenate((np.arange(1, size / 2 + 1, 2, dtype=int),
                        np.arange(size / 2 - 1, 0, -2, dtype=int)))
    f = np.zeros(size)
    f[0] = 0.25
    f[1::2] = -1 / (np.pi * n) ** 2
    return torch.tensor(2 * np.real(np.fft.fft(f)) )



class radon(torch.nn.Module):
    ''' 
    Radon Transformation

    Args:
        n_angles (int): number of projection angles for radon tranformation (default: 1000)
        image_size (int): edge length of input image (default: 400)
        device: (str): device can be either "cuda" or "cpu" (default: cuda)
    
    '''

    def __init__(self, n_angles=1000, image_size=400, device="cuda"):
        super(radon, self).__init__()
        self.n_angles=n_angles
        # get angles 
        thetas = torch.linspace(0, np.pi-(np.pi/n_angles), n_angles)[:,None,None].to(device)
        cos_al, sin_al = thetas.cos(), thetas.sin()
        zeros = torch.zeros_like(cos_al)
        # calculate rotations
        rotations = torch.stack((cos_al,sin_al,zeros,-sin_al, cos_al,zeros),-1).reshape(-1,2,3)
        self.rotated = torch.nn.functional.affine_grid(rotations, torch.Size([n_angles, 1, image_size, image_size]), align_corners=True).reshape(1,-1,image_size,2)

    def forward(self, image):
        '''Apply radon transformation on input image.

        Args:
            image (torch.tensor, (bzs, 1, W, H)): input image

        Returns:
            out (torch.tensor, (bzs, 1, W, angles)): sinogram 
        '''
        bsz, _, shape_size, _ = image.shape
        out_fl = torch.nn.functional.grid_sample(image, self.rotated.repeat(bsz,1,1,1), align_corners=True).reshape(bsz,1,self.n_angles,shape_size,shape_size)
        out = out_fl.sum(3).permute(0,1,3,2)
        return out


class fbp(torch.nn.Module):
    ''' 
    Filtered Backprojection

    Args:
        n_angles (int): number of projection angles for filtered backprojection (default: 1000)
        image_size (int): edge length of input image (default: 400)
        circle (bool): project image values outside of circle to zero (default: False)
        filtered (bool): apply filter (default: True)
        device: (str): device can be either "cuda" or "cpu" (default: cuda)
    '''
    def __init__(self, n_angles=1000, image_size=400, circle = False, filtered=True, device="cuda"):
        super().__init__()
        self.image_size=image_size
        det_count = image_size
        self.step_size = image_size/det_count
        self.n_angles = n_angles
        self.circle=circle
        self.filtered=filtered
        # padding values
        projection_size_padded = max(64, int(2 ** (2 * torch.tensor(det_count)).float().log2().ceil()))
        self.pad_width = (projection_size_padded - det_count)
        #filter
        self.filter = rampfilter(projection_size_padded).to(device)
        # get angles 
        thetas = torch.linspace(0, np.pi-(np.pi/n_angles), n_angles)[:,None,None] 
        # get grid [-1,1]
        grid_y, grid_x = torch.meshgrid(torch.linspace(-1,1,image_size), torch.linspace(-1,1,image_size))
        # get rotated grid
        tgrid = (grid_x*thetas.cos() - grid_y*thetas.sin()).unsqueeze(-1)
        y = torch.ones_like(tgrid) * torch.linspace(-1,1,n_angles)[:,None,None,None]
        self.grid = torch.cat((y,tgrid),dim=-1).view(self.n_angles * self.image_size, self.image_size, 2)[None].to(device)
        self.reconstruction_circle = (grid_x ** 2 + grid_y ** 2) <= 1

    def forward(self, input):
        '''Apply (filtered) backprojection on input sinogramm.

        Args:
            image (torch.tensor, (bzs, 1, W, angles)): sinogramm

        Returns:
            out (torch.tensor, (bzs, 1, W, H)): reconstructed image 
        '''

        bsz, _, det_count, _ = input.shape
        input = input.double()
        if self.filtered:
            # pad input
            padded_input = torch.nn.functional.pad(input, [0, 0, 0, self.pad_width], mode='constant', value=0)
            # apply filter
            projection = torch.fft.fft(padded_input,dim=2) * self.filter[:,None].double()
            radon_filtered = torch.real(torch.fft.ifft(projection,dim=2))[:, :, :det_count, :]
        else:
            radon_filtered = input
        # reconstruct
        grid = self.grid.repeat(bsz,1,1,1).double()
        reconstructed = torch.nn.functional.grid_sample(radon_filtered, grid, mode="bilinear", padding_mode='zeros', align_corners=True)
        reconstructed = reconstructed.view(bsz, self.n_angles, 1, self.image_size, self.image_size).sum(1)
        reconstructed = reconstructed/self.step_size
        # circle
        if self.circle:
            reconstructed_circle = self.reconstruction_circle.repeat(bsz,1,1,1).double()
            reconstructed[reconstructed_circle==0] = 0.
        return reconstructed  * np.pi / (2 * self.n_angles)


def get_operators(n_angles=380, image_size=400, circle = False, device='cuda'):
    ''' Creates Radon operator and Filtered Backprojection operator. 

        Args:
            n_angles (int): number of projection angles for filtered backprojection (default: 1000)
            image_size (int): edge length of input image (default: 400)
            circle (bool): project image values outside of circle to zero (default: False)
            device: (str): device can be either "cuda" or "cpu" (default: cuda)

        Returns:
            radon_op (radon): Radon operator
            fbp_op (fbp): Filtered Backprojection operator
    '''

    radon_op = radon(n_angles=n_angles, image_size=image_size, device=device)
    fbp_op = fbp(n_angles=n_angles, image_size=image_size, circle=circle, device=device)
    return radon_op, fbp_op

def test_adjoint():
    ''' Tests if Radon operator and Backprojection operator are adjoint
        by running  <radon(x),y> / <x,fbp(y)>.
    '''
    n_angles = 50
    image_size = 100
    device = 'cpu'
    # load operators
    radon_op = radon(n_angles=n_angles, image_size=image_size, device=device)
    fbp_op = fbp(n_angles=n_angles, image_size=image_size, circle=False, device=device, filtered=False)
    # run operators on random tensors
    x = torch.rand([1,1,image_size,image_size]).to(device)
    y = torch.rand([1,1,image_size,n_angles]).to(device)
    leftside = torch.sum(radon_op(x) * y).item()
    rightside = torch.sum(x * fbp_op(y)).item()
    # print
    print("\n<Ax,y>=", leftside,"  -----  <x,A'y>=", rightside)
    print('\n leftside/rightside: ',leftside/rightside)
    return leftside/rightside
