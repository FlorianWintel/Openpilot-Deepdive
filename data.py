import os
import io
import json
import torch
from math import pi
import numpy as np
import cv2
cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)

from PIL import Image
from torch.utils.data import Dataset
from torchvision import transforms
from utils import warp, generate_random_params_for_warp
from preprocess_view_transform import calibration

import utils_comma2k19.orientation as orient
import utils_comma2k19.coordinates as coord


class PlanningDataset(Dataset):
    def __init__(self, root='data', json_path_pattern='p3_%s.json', split='train'):
        self.samples = json.load(open(os.path.join(root, json_path_pattern % split)))
        print('PlanningDataset: %d samples loaded from %s' % 
              (len(self.samples), os.path.join(root, json_path_pattern % split)))
        self.split = split

        self.img_root = os.path.join(root, 'nuscenes')
        self.transforms = transforms.Compose(
            [
                # transforms.Resize((900 // 2, 1600 // 2)),
                # transforms.Resize((9 * 32, 16 * 32)),
                transforms.Resize((128, 256)),
                transforms.ToTensor(),
                transforms.Normalize([0.3890, 0.3937, 0.3851],
                                     [0.2172, 0.2141, 0.2209]),
            ]
        )

        self.enable_aug = False
        self.view_transform = False

        self.use_memcache = False
        if self.use_memcache:
            self._init_mc_()

    def _init_mc_(self):
        from petrel_client.client import Client
        self.client = Client('~/petreloss.conf')
        print('======== Initializing Memcache: Success =======')

    def _get_cv2_image(self, path):
        if self.use_memcache:
            img_bytes = self.client.get(str(path))
            assert(img_bytes is not None)
            img_mem_view = memoryview(img_bytes)
            img_array = np.frombuffer(img_mem_view, np.uint8)
            return cv2.imdecode(img_array, cv2.IMREAD_COLOR)

        else:
            return cv2.imread(path)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        sample = self.samples[idx]
        imgs, future_poses = sample['imgs'], sample['future_poses']

        # process future_poses
        future_poses = torch.tensor(future_poses)
        future_poses[:, 0] = future_poses[:, 0].clamp(1e-2, )  # the car will never go backward

        imgs = list(self._get_cv2_image(os.path.join(self.img_root, p)) for p in imgs)
        imgs = list(cv2.cvtColor(img, cv2.COLOR_BGR2RGB) for img in imgs)  # RGB

        # process images
        if self.enable_aug and self.split == 'train':
            # data augumentation when training
            # random distort (warp)
            w_offsets, h_offsets = generate_random_params_for_warp(imgs[0], random_rate=0.1)
            imgs = list(warp(img, w_offsets, h_offsets) for img in imgs)

            # random flip
            if np.random.rand() > 0.5:
                imgs = list(img[:, ::-1, :] for img in imgs)
                future_poses[:, 1] *= -1
            

        if self.view_transform:
            camera_rotation_matrix = np.linalg.inv(np.array(sample["camera_rotation_matrix_inv"]))
            camera_translation = -np.array(sample["camera_translation_inv"])
            camera_extrinsic = np.vstack((np.hstack((camera_rotation_matrix, camera_translation.reshape((3, 1)))), np.array([0, 0, 0, 1])))
            camera_extrinsic = np.linalg.inv(camera_extrinsic)
            warp_matrix = calibration(camera_extrinsic, np.array(sample["camera_intrinsic"]))
            imgs = list(cv2.warpPerspective(src = img, M = warp_matrix, dsize= (256,128), flags= cv2.WARP_INVERSE_MAP) for img in imgs)

        # cvt back to PIL images
        # cv2.imshow('0', imgs[0])
        # cv2.imshow('1', imgs[1])
        # cv2.waitKey(0)
        imgs = list(Image.fromarray(img) for img in imgs)
        imgs = list(self.transforms(img) for img in imgs)
        input_img = torch.cat(imgs, dim=0)

        return dict(
            input_img=input_img,
            future_poses=future_poses,
            camera_intrinsic=torch.tensor(sample['camera_intrinsic']),
            camera_extrinsic=torch.tensor(sample['camera_extrinsic']),
            camera_translation_inv=torch.tensor(sample['camera_translation_inv']),
            camera_rotation_matrix_inv=torch.tensor(sample['camera_rotation_matrix_inv']),
        )


class SequencePlanningDataset(PlanningDataset):
    def __init__(self, root='data', json_path_pattern='p3_%s.json', split='train'):
        print('Sequence', end='')
        self.fix_seq_length = 18
        super().__init__(root=root, json_path_pattern=json_path_pattern, split=split)

    def __getitem__(self, idx):
        seq_samples = self.samples[idx]
        seq_length = len(seq_samples)
        if seq_length < self.fix_seq_length:
            # Only 1 sample < 28 (==21)
            return self.__getitem__(np.random.randint(0, len(self.samples)))
        if seq_length > self.fix_seq_length:
            seq_length_delta = seq_length - self.fix_seq_length
            seq_length_delta = np.random.randint(0, seq_length_delta+1)
            seq_samples = seq_samples[seq_length_delta:self.fix_seq_length+seq_length_delta]

        seq_future_poses = list(smp['future_poses'] for smp in seq_samples)
        seq_imgs = list(smp['imgs'] for smp in seq_samples)

        seq_input_img = []
        for imgs in seq_imgs:
            imgs = list(self._get_cv2_image(os.path.join(self.img_root, p)) for p in imgs)
            imgs = list(cv2.cvtColor(img, cv2.COLOR_BGR2RGB) for img in imgs)  # RGB
            imgs = list(Image.fromarray(img) for img in imgs)
            imgs = list(self.transforms(img) for img in imgs)
            input_img = torch.cat(imgs, dim=0)
            seq_input_img.append(input_img[None])
        seq_input_img = torch.cat(seq_input_img)

        return dict(
            seq_input_img=seq_input_img,  # torch.Size([28, 10, 3])
            seq_future_poses=torch.tensor(seq_future_poses),  # torch.Size([28, 6, 128, 256])
            camera_intrinsic=torch.tensor(seq_samples[0]['camera_intrinsic']),
            camera_extrinsic=torch.tensor(seq_samples[0]['camera_extrinsic']),
            camera_translation_inv=torch.tensor(seq_samples[0]['camera_translation_inv']),
            camera_rotation_matrix_inv=torch.tensor(seq_samples[0]['camera_rotation_matrix_inv']),
        )


class Comma2k19SequenceDataset(PlanningDataset):
    def __init__(self, split_txt_path, prefix, num_pts=10, use_memcache=True):
        self.split_txt_path = split_txt_path
        self.prefix = prefix

        self.samples = open(split_txt_path).readlines()
        self.samples = [i.strip() for i in self.samples]

        self.num_pts = num_pts
        self.fix_seq_length = 40

        self.transforms = transforms.Compose(
            [
                # transforms.Resize((900 // 2, 1600 // 2)),
                # transforms.Resize((9 * 32, 16 * 32)),
                transforms.Resize((128, 256)),
                transforms.ToTensor(),
                transforms.Normalize([0.3890, 0.3937, 0.3851],
                                     [0.2172, 0.2141, 0.2209]),
            ]
        )

        self.use_memcache = use_memcache
        if self.use_memcache:
            self._init_mc_()

    def _get_cv2_vid(self, path):
        if self.use_memcache:
            path = self.client.get(str(path), client_method='get_object', expires_in=3600)
        return cv2.VideoCapture(path)

    def _get_numpy(self, path):
        if self.use_memcache:
            bytes = io.BytesIO(memoryview(self.client.get(str(path))))
            return np.lib.format.read_array(bytes)
        else:
            return np.load(path)

    def __getitem__(self, idx):
        seq_sample_path = self.prefix + self.samples[idx]
        cap = self._get_cv2_vid(seq_sample_path + '/video.hevc')
        if (cap.isOpened() == False):
            raise RuntimeError
        imgs = []  # <--- all frames here
        while (cap.isOpened()):
            ret, frame = cap.read()
            if ret == True:
                imgs.append(frame)
            else:
                break
        cap.release()

        seq_length = len(imgs)

        if seq_length < self.fix_seq_length + self.num_pts:
            raise RuntimeError('The length of sequence', seq_sample_path, 'is too short',
                               '(%d < %d)' % (seq_length, self.fix_seq_length + self.num_pts))
        
        seq_length_delta = seq_length - (self.fix_seq_length + self.num_pts)
        seq_length_delta = np.random.randint(1, seq_length_delta+1)

        seq_start_idx = seq_length_delta
        seq_end_idx = seq_length_delta + self.fix_seq_length

        # seq_input_img
        imgs = imgs[seq_start_idx-1: seq_end_idx]  # contains one more img
        imgs = [cv2.cvtColor(img, cv2.COLOR_BGR2RGB) for img in imgs]
        imgs = list(Image.fromarray(img) for img in imgs)
        imgs = list(self.transforms(img)[None] for img in imgs)
        input_img = torch.cat(imgs, dim=0)  # [N+1, 3, H, W]
        input_img = torch.cat((input_img[:-1, ...], input_img[1:, ...]), dim=1)

        # poses
        frame_positions = self._get_numpy(self.prefix + self.samples[idx] + '/global_pose/frame_positions')[seq_start_idx: seq_end_idx+self.num_pts]
        frame_orientations = self._get_numpy(self.prefix + self.samples[idx] + '/global_pose/frame_orientations')[seq_start_idx: seq_end_idx+self.num_pts]

        # TODO: Time-Anchor like OpenPilot
        future_poses = []
        for i in range(self.fix_seq_length):
            ecef_from_local = orient.rot_from_quat(frame_orientations[i])
            local_from_ecef = ecef_from_local.T
            frame_positions_local = np.einsum('ij,kj->ki', local_from_ecef, frame_positions - frame_positions[i]).astype(np.float32)
            
            future_poses.append(frame_positions_local[i: i+10])
        future_poses = torch.tensor(future_poses)

        return dict(
            seq_input_img=input_img,  # torch.Size([N, 6, 128, 256])
            seq_future_poses=future_poses,  # torch.Size([N, num_pts, 3])
            # camera_intrinsic=torch.tensor(seq_samples[0]['camera_intrinsic']),
            # camera_extrinsic=torch.tensor(seq_samples[0]['camera_extrinsic']),
            # camera_translation_inv=torch.tensor(seq_samples[0]['camera_translation_inv']),
            # camera_rotation_matrix_inv=torch.tensor(seq_samples[0]['camera_rotation_matrix_inv']),
        )


if __name__ == '__main__':
    from torch.utils.data import DataLoader
    from tqdm import tqdm
    from utils import draw_trajectory_on_ax
    import matplotlib.pyplot as plt

    dataset = Comma2k19SequenceDataset('data/comma2k19_val_non_overlap.txt', 'data/comma2k19/', use_memcache=False)
    for sample in dataset:
        for k, v in sample.items():
            print(k, ':', v.shape, v.dtype)
        for img, traj in zip(sample['seq_input_img'], sample['seq_future_poses']):
            trajectories = list(traj.numpy().reshape(1, 10, 3))  # M, num_pts, 3
            print(trajectories)

            fig, ax = plt.subplots()
            ax = draw_trajectory_on_ax(ax, trajectories, [1, ])
            plt.tight_layout()
            plt.show()


    exit()

    dataset = PlanningDataset(split='val')

    calculate_image_mean_std = False
    if calculate_image_mean_std:
        # Calculating the stats of the images
        psum    = torch.tensor([0.0, 0.0, 0.0])
        psum_sq = torch.tensor([0.0, 0.0, 0.0])

        # loop through images
        for img, _ in tqdm(dataset):
            inputs = img[0:3]
            inputs = inputs[None]
            psum    += inputs.sum(axis        = [0, 2, 3])
            psum_sq += (inputs ** 2).sum(axis = [0, 2, 3])
        # pixel count
        count = len(dataset) * 900 * 1600

        # mean and std
        total_mean = psum / count
        total_var  = (psum_sq / count) - (total_mean ** 2)
        total_std  = torch.sqrt(total_var)

        # output
        print('mean: '  + str(total_mean))  # [0.3890, 0.3937, 0.3851]
        print('std:  '  + str(total_std))   # [0.2172, 0.2141, 0.2209]

    calculate_poses_mean_std = True
    if calculate_poses_mean_std:
        import matplotlib.pyplot as plt

        poses_list = []
        for _, poses in tqdm(DataLoader(dataset, shuffle=True)):
            poses_list += list(p.numpy()[None, ] for p in poses[0])
            if len(poses_list) > 40000:
                break

        poses = np.concatenate(poses_list, axis=0)
        print('mean: ', np.mean(poses, axis=0))
        print('std:  ', np.std(poses, axis=0))

        plt.hist(poses[:, 0], bins=200, )
        plt.show()

        plt.hist(poses[:, 1], bins=200, )
        plt.show()

        plt.hist(poses[:, 2], bins=200, )
        plt.show()

    exit()

    dataloader = DataLoader(dataset, batch_size=8, shuffle=True)

    for img, poses in dataloader:
        print(img.shape, img.max(), img.min(), img.mean())
        print(poses.shape, poses.max(), poses.min(), poses.mean())
