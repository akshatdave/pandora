import os
import torch
import numpy as np
from tqdm import tqdm

import sys
sys.path.append('./')
from utils.io_util import load_mask, load_rgb_exr, load_normal_exr, glob_imgs
from utils.rend_util import rot_to_quat, load_K_Rt_from_P

class SceneDataset(torch.utils.data.Dataset):
    # NOTE: jianfei: modified from IDR.   https://github.com/lioryariv/idr/blob/main/code/datasets/scene_dataset.py
    """Dataset for a class of objects, where each datapoint is a SceneInstanceDataset."""

    def __init__(self,
                 train_cameras,
                 data_dir,
                 downscale=1.,   # [H, W]
                 cam_file=None,
                 scale_radius=-1,
                 load_normals=True,
                 sel_idx_offset=None,
                 sel_idx_sample=None):

        assert os.path.exists(data_dir), "Data directory is empty"

        self.instance_dir = data_dir
        self.train_cameras = train_cameras
        self.load_normals = load_normals

        image_dir = '{0}/image'.format(self.instance_dir)
        image_paths = sorted(glob_imgs(image_dir))
        mask_dir = '{0}/mask'.format(self.instance_dir)
        mask_paths = sorted(glob_imgs(mask_dir))
        if self.load_normals:
            normal_dir = '{0}/normal'.format(self.instance_dir)
            normal_paths = sorted(glob_imgs(normal_dir))
        # If dataset has specular
        specular_dir = '{0}/specular'.format(self.instance_dir)
        if os.path.exists(specular_dir):
            self.only_diffuse = False
            specular_paths = sorted(glob_imgs(specular_dir))
        else:
            self.only_diffuse = True


        self.n_images = len(image_paths)

        # determine width, height
        self.downscale = downscale
        tmp_rgb = load_rgb_exr(image_paths[0], downscale)
        _, self.H, self.W = tmp_rgb.shape
        

        self.cam_file = '{0}/cameras.npz'.format(self.instance_dir)
        if cam_file is not None:
            self.cam_file = '{0}/{1}'.format(self.instance_dir, cam_file)

        camera_dict = np.load(self.cam_file)
        scale_mats = [camera_dict['scale_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]
        world_mats = [camera_dict['world_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]

        sel_idx = range(len(image_paths))
        if sel_idx_offset is not None:
            sel_idx = sel_idx[sel_idx_offset::sel_idx_sample]
        self.n_images = len(sel_idx)
        print('IMAGES SELECTED: ',self.n_images)

        self.intrinsics_all = []
        self.c2w_all = []
        cam_center_norms = []
        idx =0 
        for scale_mat, world_mat in zip(scale_mats, world_mats):
            if idx not in sel_idx:
                idx += 1
                continue
            idx += 1
            P = world_mat @ scale_mat
            P = P[:3, :4]
            intrinsics, pose = load_K_Rt_from_P(P)
            cam_center_norms.append(np.linalg.norm(pose[:3,3]))
            
            # Change intrinsics to match convention
            intrinsics[0,0] = -intrinsics[0,0]
            intrinsics[1,1] = -intrinsics[1,1]
            
            # downscale intrinsics
            intrinsics[0, 2] /= downscale
            intrinsics[1, 2] /= downscale
            intrinsics[0, 0] /= downscale
            intrinsics[1, 1] /= downscale
            # intrinsics[0, 1] /= downscale # skew is a ratio, do not scale
            
            self.intrinsics_all.append(torch.from_numpy(intrinsics).float())
            self.c2w_all.append(torch.from_numpy(pose).float())
        max_cam_norm = max(cam_center_norms)
        if scale_radius > 0:
            for i in range(len(self.c2w_all)):
                self.c2w_all[i][:3, 3] *= (scale_radius / max_cam_norm / 1.1)

        self.rgb_images = []
        idx =0 
        for path in tqdm(image_paths, desc='loading images...'):
            if idx not in sel_idx:
                idx += 1
                continue
            idx += 1
            rgb = load_rgb_exr(path, downscale)
            rgb = rgb.reshape(3, -1).transpose(1, 0)
            self.rgb_images.append(torch.from_numpy(rgb).float())

        self.object_masks = []
        idx =0 
        for path in mask_paths:
            if idx not in sel_idx:
                idx += 1
                continue
            idx += 1
            object_mask = load_mask(path, downscale)
            object_mask = object_mask.reshape(-1)
            self.object_masks.append(torch.from_numpy(object_mask).to(dtype=torch.bool))

        if self.load_normals:
            self.normal_images = []
            idx =0 
            for path in normal_paths:
                if idx not in sel_idx:
                    idx += 1
                    continue
                idx += 1
                normal = load_normal_exr(path, downscale)
                normal = normal.reshape(3,-1).transpose(1,0)
                self.normal_images.append(torch.from_numpy(normal).float())
        
        if not self.only_diffuse:
            self.specular_images = []
            idx =0 
            for path in specular_paths:
                if idx not in sel_idx:
                    idx += 1
                    continue
                idx += 1
                specular = load_rgb_exr(path, downscale)
                specular = specular.reshape(3, -1).transpose(1,0)
                self.specular_images.append(torch.from_numpy(specular).float())


    def __len__(self):
        return self.n_images

    def __getitem__(self, idx):
        # uv = np.mgrid[0:self.img_res[0], 0:self.img_res[1]].astype(np.int32)
        # uv = torch.from_numpy(np.flip(uv, axis=0).copy()).float()
        # uv = uv.reshape(2, -1).transpose(1, 0)

        sample = {
            "object_mask": self.object_masks[idx],
            "intrinsics": self.intrinsics_all[idx],
        }

        ground_truth = {
            "rgb": self.rgb_images[idx],
        }
        if self.load_normals:
            ground_truth["normal"] = self.normal_images[idx]
        
        if not self.only_diffuse:
            ground_truth["specular"] = self.specular_images[idx]

        ground_truth["rgb"] = self.rgb_images[idx]
        sample["object_mask"] = self.object_masks[idx]

        if not self.train_cameras:
            sample["c2w"] = self.c2w_all[idx]

        return idx, sample, ground_truth

    def collate_fn(self, batch_list):
        # get list of dictionaries and returns input, ground_true as dictionary for all batch instances
        batch_list = zip(*batch_list)

        all_parsed = []
        for entry in batch_list:
            if type(entry[0]) is dict:
                # make them all into a new dict
                ret = {}
                for k in entry[0].keys():
                    ret[k] = torch.stack([obj[k] for obj in entry])
                all_parsed.append(ret)
            else:
                all_parsed.append(torch.LongTensor(entry))

        return tuple(all_parsed)

    def get_scale_mat(self):
        return np.load(self.cam_file)['scale_mat_0']

    def get_gt_pose(self, scaled=True):
        #TODO: Currently doesn't work when subsampling views
        # Load gt pose without normalization to unit sphere
        camera_dict = np.load(self.cam_file)
        world_mats = [camera_dict['world_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]
        scale_mats = [camera_dict['scale_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]

        c2w_all = []
        for scale_mat, world_mat in zip(scale_mats, world_mats):
            P = world_mat
            if scaled:
                P = world_mat @ scale_mat
            P = P[:3, :4]
            _, pose = load_K_Rt_from_P(P)
            c2w_all.append(torch.from_numpy(pose).float())

        return torch.cat([p.float().unsqueeze(0) for p in c2w_all], 0)

    def get_pose_init(self):
        # get noisy initializations obtained with the linear method
        cam_file = '{0}/cameras_linear_init.npz'.format(self.instance_dir)
        camera_dict = np.load(cam_file)
        scale_mats = [camera_dict['scale_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]
        world_mats = [camera_dict['world_mat_%d' % idx].astype(np.float32) for idx in range(self.n_images)]

        init_pose = []
        for scale_mat, world_mat in zip(scale_mats, world_mats):
            P = world_mat @ scale_mat
            P = P[:3, :4]
            _, pose = load_K_Rt_from_P(P)
            init_pose.append(pose)
        init_pose = torch.cat([torch.Tensor(pose).float().unsqueeze(0) for pose in init_pose], 0).cuda()
        init_quat = rot_to_quat(init_pose[:, :3, :3])
        init_quat = torch.cat([init_quat, init_pose[:, :3, 3]], 1)

        return init_quat

if __name__ == "__main__":
    # dataset = SceneDataset(False, './data/DTU/scan65')
    dataset = SceneDataset(False, './data/mitsuba2_renders/sphere_christmas_v11_a=0p04')
    # dataset = SceneDataset(False, 
    #                        './data/mitsuba2_renders/bunny_v17_3m_depth_N_res_800_49',
    #                        sel_idx_offset=0, sel_idx_sample=3)
    c2w = dataset.get_gt_pose(scaled=True).data.cpu().numpy()
    extrinsics = np.linalg.inv(c2w)  # camera extrinsics are w2c matrix
    import pdb; pdb.set_trace()
    camera_matrix = next(iter(dataset))[1]['intrinsics'].data.cpu().numpy()
    from tools.vis_camera import visualize
    visualize(camera_matrix, extrinsics,label='mitsuba2_new')
    import pdb;pdb.set_trace()