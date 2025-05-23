import bisect
import math
import random
from dataclasses import dataclass, field

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, IterableDataset

import threestudio
from threestudio import register
from threestudio.utils.base import Updateable
from threestudio.utils.config import parse_structured
from threestudio.utils.misc import get_device
from threestudio.utils.ops import (
    get_mvp_matrix,
    get_projection_matrix,
    get_ray_directions,
    get_rays,
)
from threestudio.utils.typing import *
from gaussiansplatting.scene.cameras import Camera_HumanGaussian
import os
import numpy as np

def safe_normalize(x, eps=1e-20):
    return x / torch.sqrt(torch.clamp(torch.sum(x * x, -1, keepdim=True), min=eps))

def convert_camera_to_world_transform(transform):
    # 将右手坐标系的变换矩阵转换为左手坐标系
    
    # 复制原始变换矩阵
    converted_transform = transform.clone()
    
    # 反转观察方向（将平移分量的第三个元素乘以-1）
    converted_transform[:, 2] *= -1
    
    # 交换第一行和第三行
    converted_transform[[0, 2], :] = converted_transform[[2, 0], :]
    
    return converted_transform

def circle_poses(device, radius=torch.tensor([3.2]), theta=torch.tensor([60]), phi=torch.tensor([0])):

    theta = theta / 180 * np.pi
    phi = phi / 180 * np.pi

    centers = torch.stack([
        radius * torch.sin(theta) * torch.sin(phi),
        radius * torch.cos(theta),
        radius * torch.sin(theta) * torch.cos(phi),
    ], dim=-1) # [B, 3]

    # lookat
    forward_vector = safe_normalize(centers)
    up_vector = torch.FloatTensor([0, 1, 0]).to(device).unsqueeze(0).repeat(len(centers), 1)
    right_vector = safe_normalize(torch.cross(forward_vector, up_vector, dim=-1))
    up_vector = safe_normalize(torch.cross(right_vector, forward_vector, dim=-1))

    poses = torch.eye(4, dtype=torch.float, device=device).unsqueeze(0).repeat(len(centers), 1, 1)
    poses[:, :3, :3] = torch.stack((right_vector, up_vector, forward_vector), dim=-1)
    poses[:, :3, 3] = centers

    return poses

trans_t = lambda t : torch.Tensor([
    [1,0,0,0],
    [0,1,0,0],
    [0,0,1,t],
    [0,0,0,1]]).float()

rot_phi = lambda phi : torch.Tensor([
    [1,0,0,0],
    [0,np.cos(phi),-np.sin(phi),0],
    [0,np.sin(phi), np.cos(phi),0],
    [0,0,0,1]]).float()

rot_theta = lambda th : torch.Tensor([
    [np.cos(th),0,-np.sin(th),0],
    [0,1,0,0],
    [np.sin(th),0, np.cos(th),0],
    [0,0,0,1]]).float()

def rodrigues_mat_to_rot(R):
  eps =1e-16
  trc = np.trace(R)
  trc2 = (trc - 1.)/ 2.
  #sinacostrc2 = np.sqrt(1 - trc2 * trc2)
  s = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
  if (1 - trc2 * trc2) >= eps:
    tHeta = np.arccos(trc2)
    tHetaf = tHeta / (2 * (np.sin(tHeta)))
  else:
    tHeta = np.real(np.arccos(trc2))
    tHetaf = 0.5 / (1 - tHeta / 6)
  omega = tHetaf * s
  return omega

def rodrigues_rot_to_mat(r):
  wx,wy,wz = r
  theta = np.sqrt(wx * wx + wy * wy + wz * wz)
  a = np.cos(theta)
  b = (1 - np.cos(theta)) / (theta*theta)
  c = np.sin(theta) / theta
  R = np.zeros([3,3])
  R[0, 0] = a + b * (wx * wx)
  R[0, 1] = b * wx * wy - c * wz
  R[0, 2] = b * wx * wz + c * wy
  R[1, 0] = b * wx * wy + c * wz
  R[1, 1] = a + b * (wy * wy)
  R[1, 2] = b * wy * wz - c * wx
  R[2, 0] = b * wx * wz - c * wy
  R[2, 1] = b * wz * wy + c * wx
  R[2, 2] = a + b * (wz * wz)
  return R


def pose_spherical(theta, phi, radius):
    c2w = trans_t(radius)
    c2w = rot_phi(phi/180.*np.pi) @ c2w
    c2w = rot_theta(theta/180.*np.pi) @ c2w
    c2w = torch.Tensor(np.array([[-1,0,0,0],[0,0,1,0],[0,1,0,0],[0,0,0,1]])) @ c2w
    return c2w

def convert_camera_pose(camera_pose):
    # Clone the tensor to avoid in-place operations
    colmap_pose = camera_pose.clone()

    # Extract rotation and translation components
    rotation = colmap_pose[:, :3, :3]
    translation = colmap_pose[:, :3, 3]

    # Change rotation orientation
    rotation[:, 0, :] *= -1
    rotation[:, 1, :] *= -1

    # Change translation position
    translation[:, 0] *= -1
    translation[:, 1] *= -1

    return colmap_pose

def convert_camera_pose(camera_pose):
    # Clone the tensor to avoid in-place operations
    colmap_pose = camera_pose.clone()

    # Extract rotation and translation components
    rotation = colmap_pose[:, :3, :3]
    translation = colmap_pose[:, :3, 3]

    # Change rotation orientation
    rotation[:, 0, :] *= -1
    rotation[:, 1, :] *= -1

    # Change translation position
    translation[:, 0] *= -1
    translation[:, 1] *= -1
    
    return colmap_pose

@dataclass
class RandomCameraDataModuleConfig:
    # height, width, and batch_size should be Union[int, List[int]]
    # but OmegaConf does not support Union of containers
    source: str = None
    max_view_num: int = 48
    azimuth_view_num: int = 8
    elevation_view_num: int = 2
    
    
    
    
    
    
    height: Any = 512
    width: Any = 512
    batch_size: Any = 1
    resolution_milestones: List[int] = field(default_factory=lambda: [])
    eval_height: int = 512
    eval_width: int = 512
    eval_batch_size: int = 1
    n_val_views: int = 4
    n_test_views: int = 120
    # elevation_range: Tuple[float, float] = (-30, 60)
    elevation_range: Tuple[float, float] = (10 , 10 )
    azimuth_range: Tuple[float, float] = (-180, 180)
    # azimuth_range: Tuple[float, float] = (+45, 135)
    # azimuth_range: Tuple[float, float] = (+55, 125)
    camera_distance_range: Tuple[float, float] = (1,1.)
    # fovy_range: Tuple[float, float] = (
    #     40,
    #     70,
    # )  # in degrees, in vertical direction (along height)
    fovy_range: Tuple[float, float] = (
        55,
        55,
    )  # in degrees, in vertical direction (along height)
    
    camera_perturb: float = 0.
    center_perturb: float = 0.
    up_perturb: float = 0.0
    light_position_perturb: float = 1.0
    light_distance_range: Tuple[float, float] = (0.8, 1.5)
    # eval_elevation_deg: float = 15.0
    eval_elevation_deg: float = 10.
    # eval_camera_distance: float = 6.
    eval_camera_distance: float = 1.
    # eval_fovy_deg: float = 70.0
    eval_fovy_deg: float = 55.
    light_sample_strategy: str = "dreamfusion"
    batch_uniform_azimuth: bool = True
    progressive_until: int = 0  # progressive ranges for elevation, azimuth, r, fovy

    # near head pose
    # enable_near_head_poses: bool = False
    # enable_near_back_poses: bool = False
    # head_offset: float = 0.65
    # back_offset: float = 0.65
    # head_camera_distance_range: Tuple[float, float] = (0.4, 0.6)
    # back_camera_distance_range: Tuple[float, float] = (0.6, 0.8)
    # head_prob: float = 0.25
    # head_start_step: int = 1200
    # head_end_step: int = 3600
    # head_azimuth_range: Tuple[float, float] = (0, 180)
    # back_prob: float = 0.20
    # back_start_step: int = 1200
    # back_end_step: int = 3600
    # back_azimuth_range: Tuple[float, float] = (-180, 0)
    frontal_prob: float = 0.0
    # frontal_azimuth_range: Tuple[float, float] = (45, 135)

class RandomCameraIterableDataset(IterableDataset, Updateable):
    def __init__(self, cfg: Any) -> None:
        super().__init__()
        self.cfg: RandomCameraDataModuleConfig = cfg
        self.heights: List[int] = (
            [self.cfg.height] if isinstance(self.cfg.height, int) else self.cfg.height
        )
        self.widths: List[int] = (
            [self.cfg.width] if isinstance(self.cfg.width, int) else self.cfg.width
        )
        self.batch_sizes: List[int] = (
            [self.cfg.batch_size]
            if isinstance(self.cfg.batch_size, int)
            else self.cfg.batch_size
        )
        assert len(self.heights) == len(self.widths) == len(self.batch_sizes)
        self.resolution_milestones: List[int]
        if (
            len(self.heights) == 1
            and len(self.widths) == 1
            and len(self.batch_sizes) == 1
        ):
            if len(self.cfg.resolution_milestones) > 0:
                threestudio.warn(
                    "Ignoring resolution_milestones since height and width are not changing"
                )
            self.resolution_milestones = [-1]
        else:
            assert len(self.heights) == len(self.cfg.resolution_milestones) + 1
            self.resolution_milestones = [-1] + self.cfg.resolution_milestones

        self.directions_unit_focals = [
            get_ray_directions(H=height, W=width, focal=1.0)
            for (height, width) in zip(self.heights, self.widths)
        ]
        self.height: int = self.heights[0]
        self.width: int = self.widths[0]
        self.batch_size: int = self.batch_sizes[0]
        self.directions_unit_focal = self.directions_unit_focals[0]
        self.elevation_range = self.cfg.elevation_range
        self.azimuth_range = self.cfg.azimuth_range
        self.camera_distance_range = self.cfg.camera_distance_range
        # self.head_camera_distance_range = self.cfg.head_camera_distance_range
        # self.back_camera_distance_range = self.cfg.back_camera_distance_range
        self.fovy_range = self.cfg.fovy_range
        self.cur_step = 0


        self.total_view_num=200
        self.n2n_view_index = random.sample(
            range(0, self.total_view_num),
            min(self.total_view_num, self.cfg.max_view_num),
        )
                
        self.generate_fixed_batch()
        

    def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
        size_ind = bisect.bisect_right(self.resolution_milestones, global_step) - 1
        self.height = self.heights[size_ind]
        self.width = self.widths[size_ind]
        self.batch_size = self.batch_sizes[size_ind]
        self.directions_unit_focal = self.directions_unit_focals[size_ind]
        threestudio.debug(
            f"Training height: {self.height}, width: {self.width}, batch_size: {self.batch_size}"
        )
        self.cur_step = global_step
        # progressive view
        self.progressive_view(global_step)

    def __iter__(self):
        while True:
            yield {}

    def progressive_view(self, global_step):
        pass

        # r = min(1.0, global_step / (self.cfg.progressive_until + 1))
        # self.elevation_range = [
        #     (1 - r) * self.cfg.eval_elevation_deg + r * self.cfg.elevation_range[0],
        #     (1 - r) * self.cfg.eval_elevation_deg + r * self.cfg.elevation_range[1],
        # ]
        # self.azimuth_range = [
        #     (1 - r) * 0.0 + r * self.cfg.azimuth_range[0],
        #     (1 - r) * 0.0 + r * self.cfg.azimuth_range[1],
        # ]

        # self.camera_distance_range = [
        #     (1 - r) * self.cfg.eval_camera_distance
        #     + r * self.cfg.camera_distance_range[0],
        #     (1 - r) * self.cfg.eval_camera_distance
        #     + r * self.cfg.camera_distance_range[1],
        # ]
        # self.fovy_range = [
        #     (1 - r) * self.cfg.eval_fovy_deg + r * self.cfg.fovy_range[0],
        #     (1 - r) * self.cfg.eval_fovy_deg + r * self.cfg.fovy_range[1],
        # ]

    def generate_fixed_batch(self) -> Dict[str, Any]:
        self.temp_dataset={}
        for id in self.n2n_view_index:
            # random head zoom-in
            # if self.cfg.enable_near_head_poses and random.random() < self.cfg.head_prob and self.cur_step >= self.cfg.head_start_step and self.cur_step <= self.cfg.head_end_step:
            if False:
                zoom_in_head = True
                zoom_in_back = False
                camera_distance_range = self.head_camera_distance_range
                self.azimuth_range = self.cfg.head_azimuth_range
            # elif self.cfg.enable_near_back_poses and random.random() < self.cfg.back_prob and self.cur_step >= self.cfg.back_start_step and self.cur_step <= self.cfg.back_end_step:
            elif False:
                zoom_in_head = False
                zoom_in_back = True
                camera_distance_range = self.back_camera_distance_range
                self.azimuth_range = self.cfg.back_azimuth_range
            else:
                zoom_in_head = False
                zoom_in_back = False
                camera_distance_range = self.camera_distance_range
                if random.random() < self.cfg.frontal_prob:
                    self.azimuth_range = self.cfg.frontal_azimuth_range
                else:    
                    self.azimuth_range = self.cfg.azimuth_range
            
            # sample elevation angles
            elevation_deg: Float[Tensor, "B"]
            elevation: Float[Tensor, "B"]
            # if random.random() < 0.5:
            if False:
                # sample elevation angles uniformly with a probability 0.5 (biased towards poles)
                elevation_deg = (
                    torch.rand(self.batch_size)
                    * (self.elevation_range[1] - self.elevation_range[0])
                    + self.elevation_range[0]
                )
                elevation = elevation_deg * math.pi / 180
            else:
                # otherwise sample uniformly on sphere
                elevation_range_percent = [
                    (self.elevation_range[0] + 90.0) / 180.0,
                    (self.elevation_range[1] + 90.0) / 180.0,
                ]
                # inverse transform sampling
                elevation = torch.asin(
                    2
                    * (
                        torch.rand(self.batch_size)
                        * (elevation_range_percent[1] - elevation_range_percent[0])
                        + elevation_range_percent[0]
                    )
                    - 1.0
                )
                elevation_deg = elevation / math.pi * 180.0

            # sample azimuth angles from a uniform distribution bounded by azimuth_range
            azimuth_deg: Float[Tensor, "B"]
            if self.cfg.batch_uniform_azimuth:
                # ensures sampled azimuth angles in a batch cover the whole range
                azimuth_deg = (
                    torch.rand(self.batch_size) + torch.arange(self.batch_size)
                ) / self.batch_size * (
                    self.azimuth_range[1] - self.azimuth_range[0]
                ) + self.azimuth_range[
                    0
                ]
            else:
                # simple random sampling
                azimuth_deg = (
                    torch.rand(self.batch_size)
                    * (self.azimuth_range[1] - self.azimuth_range[0])
                    + self.azimuth_range[0]
                )
            azimuth = azimuth_deg * math.pi / 180

            # sample distances from a uniform distribution bounded by distance_range
            camera_distances: Float[Tensor, "B"] = (
                torch.rand(self.batch_size)
                * (camera_distance_range[1] - camera_distance_range[0])
                + camera_distance_range[0]
            )

            # convert spherical coordinates to cartesian coordinates
            # right hand coordinate system, x back, y right, z up
            # elevation in (-90, 90), azimuth from +x to +y in (-180, 180)
            camera_positions: Float[Tensor, "B 3"] = torch.stack(
                [
                    camera_distances * torch.cos(elevation) * torch.cos(azimuth),
                    camera_distances * torch.cos(elevation) * torch.sin(azimuth),
                    camera_distances * torch.sin(elevation),
                ],
                dim=-1,
            )

            # default scene center at origin
            center: Float[Tensor, "B 3"] = torch.zeros_like(camera_positions)

            if zoom_in_head:
                # z-axis add offset to move the camera centered around head
                center[:, 2] += self.cfg.head_offset
                camera_positions[:, 2] += self.cfg.head_offset
            elif zoom_in_back:
                # z-axis add offset to move the camera centered around head
                center[:, 2] += self.cfg.back_offset
                camera_positions[:, 2] += self.cfg.back_offset

            # default camera up direction as +z
            up: Float[Tensor, "B 3"] = torch.as_tensor([0, 0, 1], dtype=torch.float32)[
                None, :
            ].repeat(self.batch_size, 1)

            # sample camera perturbations from a uniform distribution [-camera_perturb, camera_perturb]
            camera_perturb: Float[Tensor, "B 3"] = (
                torch.rand(self.batch_size, 3) * 2 * self.cfg.camera_perturb
                - self.cfg.camera_perturb
            )
            camera_positions = camera_positions + camera_perturb
            # sample center perturbations from a normal distribution with mean 0 and std center_perturb
            center_perturb: Float[Tensor, "B 3"] = (
                torch.randn(self.batch_size, 3) * self.cfg.center_perturb
            )
            center = center + center_perturb
            # sample up perturbations from a normal distribution with mean 0 and std up_perturb
            up_perturb: Float[Tensor, "B 3"] = (
                torch.randn(self.batch_size, 3) * self.cfg.up_perturb
            )
            up = up + up_perturb

            # sample fovs from a uniform distribution bounded by fov_range
            fovy_deg: Float[Tensor, "B"] = (
                torch.rand(self.batch_size) * (self.fovy_range[1] - self.fovy_range[0])
                + self.fovy_range[0]
            )
            fovy = fovy_deg * math.pi / 180

            # sample light distance from a uniform distribution bounded by light_distance_range
            light_distances: Float[Tensor, "B"] = (
                torch.rand(self.batch_size)
                * (self.cfg.light_distance_range[1] - self.cfg.light_distance_range[0])
                + self.cfg.light_distance_range[0]
            )

            if self.cfg.light_sample_strategy == "dreamfusion" or self.cfg.light_sample_strategy == "dreamfusion3dgs":
                # sample light direction from a normal distribution with mean camera_position and std light_position_perturb
                light_direction: Float[Tensor, "B 3"] = F.normalize(
                    camera_positions
                    + torch.randn(self.batch_size, 3) * self.cfg.light_position_perturb,
                    dim=-1,
                )
                # get light position by scaling light direction by light distance
                light_positions: Float[Tensor, "B 3"] = (
                    light_direction * light_distances[:, None]
                )
            elif self.cfg.light_sample_strategy == "magic3d":
                # sample light direction within restricted angle range (pi/3)
                local_z = F.normalize(camera_positions, dim=-1)
                local_x = F.normalize(
                    torch.stack(
                        [local_z[:, 1], -local_z[:, 0], torch.zeros_like(local_z[:, 0])],
                        dim=-1,
                    ),
                    dim=-1,
                )
                local_y = F.normalize(torch.cross(local_z, local_x, dim=-1), dim=-1)
                rot = torch.stack([local_x, local_y, local_z], dim=-1)
                light_azimuth = (
                    torch.rand(self.batch_size) * math.pi * 2 - math.pi
                )  # [-pi, pi]
                light_elevation = (
                    torch.rand(self.batch_size) * math.pi / 3 + math.pi / 6
                )  # [pi/6, pi/2]
                light_positions_local = torch.stack(
                    [
                        light_distances
                        * torch.cos(light_elevation)
                        * torch.cos(light_azimuth),
                        light_distances
                        * torch.cos(light_elevation)
                        * torch.sin(light_azimuth),
                        light_distances * torch.sin(light_elevation),
                    ],
                    dim=-1,
                )
                light_positions = (rot @ light_positions_local[:, :, None])[:, :, 0]
            else:
                raise ValueError(
                    f"Unknown light sample strategy: {self.cfg.light_sample_strategy}"
                )

            lookat: Float[Tensor, "B 3"] = F.normalize(center - camera_positions, dim=-1)
            right: Float[Tensor, "B 3"] = F.normalize(torch.cross(lookat, up), dim=-1)
            up = F.normalize(torch.cross(right, lookat), dim=-1)
            c2w3x4: Float[Tensor, "B 3 4"] = torch.cat(
                [torch.stack([right, up, -lookat], dim=-1), camera_positions[:, :, None]],
                dim=-1,
            )
            c2w: Float[Tensor, "B 4 4"] = torch.cat(
                [c2w3x4, torch.zeros_like(c2w3x4[:, :1])], dim=1
            )
            c2w[:, 3, 3] = 1.0

            # get directions by dividing directions_unit_focal by focal length
            focal_length: Float[Tensor, "B"] = 0.5 * self.height / torch.tan(0.5 * fovy)
            directions: Float[Tensor, "B H W 3"] = self.directions_unit_focal[
                None, :, :, :
            ].repeat(self.batch_size, 1, 1, 1)
            directions[:, :, :, :2] = (
                directions[:, :, :, :2] / focal_length[:, None, None, None]
            )

            proj_mtx: Float[Tensor, "B 4 4"] = get_projection_matrix(
                fovy, self.width / self.height, 0.1, 1000.0
            )  # FIXME: hard-coded near and far
            mvp_mtx: Float[Tensor, "B 4 4"] = get_mvp_matrix(c2w, proj_mtx)
            
            # print('c2w:',c2w.shape)
            self.temp_dataset[id]={
                "index" : id,
                "camera": Camera_HumanGaussian(c2w=c2w[0],FoVy=fovy,height=self.height,width=self.width),
                "height": self.height,
                "width": self.width,
                
            }


            # return {
            #     "mvp_mtx": mvp_mtx,
            #     "camera_positions": camera_positions,
            #     "c2w": c2w,
            #     "light_positions": light_positions,
            #     "elevation": elevation_deg,
            #     "azimuth": azimuth_deg,
            #     "camera_distances": camera_distances,
            #     "height": self.height,
            #     "width": self.width,
            #     "fovy":fovy,

            # }

    def collate(self, batch) -> Dict[str, Any]:
        selected_dict = random.choice(list(self.temp_dataset.items()))[-1]
        return {
            "index": [selected_dict["index"]],
            "camera": [selected_dict["camera"]],
            "height": self.height,
            "width": self.width,
        }

class RandomCameraDataset(Dataset):
    def __init__(self, cfg: Any, split: str) -> None:
        super().__init__()
        self.cfg: RandomCameraDataModuleConfig = cfg
        self.split = split

        if split == "val":
            self.n_views = self.cfg.n_val_views
        else:
            self.n_views = self.cfg.n_test_views

        azimuth_deg: Float[Tensor, "B"]
        if self.split == "val":
            # make sure the first and last view are not the same
            # azimuth_deg = torch.linspace(-180., 180.0, self.n_views + 1)[: self.n_views]
            azimuth_deg = torch.linspace(55., 125.0, self.n_views + 1)[: self.n_views]
        else:
            # azimuth_deg = torch.linspace(-180., 180.0, self.n_views)
            azimuth_deg = torch.linspace(55., 125.0, self.n_views)
        elevation_deg: Float[Tensor, "B"] = torch.full_like(
            azimuth_deg, self.cfg.eval_elevation_deg
        )
        camera_distances: Float[Tensor, "B"] = torch.full_like(
            elevation_deg, self.cfg.eval_camera_distance
        )

        elevation = elevation_deg * math.pi / 180
        azimuth = azimuth_deg * math.pi / 180

        # convert spherical coordinates to cartesian coordinates
        # right hand coordinate system, x back, y right, z up
        # elevation in (-90, 90), azimuth from +x to +y in (-180, 180)
        camera_positions: Float[Tensor, "B 3"] = torch.stack(
            [
                camera_distances * torch.cos(elevation) * torch.cos(azimuth),
                camera_distances * torch.cos(elevation) * torch.sin(azimuth),
                camera_distances * torch.sin(elevation),
            ],
            dim=-1,
        )

        # default scene center at origin
        center: Float[Tensor, "B 3"] = torch.zeros_like(camera_positions)
        # default camera up direction as +z
        up: Float[Tensor, "B 3"] = torch.as_tensor([0, 0, 1], dtype=torch.float32)[
            None, :
        ].repeat(self.cfg.eval_batch_size, 1)

        fovy_deg: Float[Tensor, "B"] = torch.full_like(
            elevation_deg, self.cfg.eval_fovy_deg
        )
        fovy = fovy_deg * math.pi / 180
        light_positions: Float[Tensor, "B 3"] = camera_positions

        lookat: Float[Tensor, "B 3"] = F.normalize(center - camera_positions, dim=-1)
        right: Float[Tensor, "B 3"] = F.normalize(torch.cross(lookat, up), dim=-1)
        up = F.normalize(torch.cross(right, lookat), dim=-1)
        c2w3x4: Float[Tensor, "B 3 4"] = torch.cat(
            [torch.stack([right, up, -lookat], dim=-1), camera_positions[:, :, None]],
            dim=-1,
        )
        c2w: Float[Tensor, "B 4 4"] = torch.cat(
            [c2w3x4, torch.zeros_like(c2w3x4[:, :1])], dim=1
        )
        c2w[:, 3, 3] = 1.0

        # get directions by dividing directions_unit_focal by focal length
        focal_length: Float[Tensor, "B"] = (
            0.5 * self.cfg.eval_height / torch.tan(0.5 * fovy)
        )
        directions_unit_focal = get_ray_directions(
            H=self.cfg.eval_height, W=self.cfg.eval_width, focal=1.0
        )
        directions: Float[Tensor, "B H W 3"] = directions_unit_focal[
            None, :, :, :
        ].repeat(self.n_views, 1, 1, 1)
        directions[:, :, :, :2] = (
            directions[:, :, :, :2] / focal_length[:, None, None, None]
        )

        proj_mtx: Float[Tensor, "B 4 4"] = get_projection_matrix(
            fovy, self.cfg.eval_width / self.cfg.eval_height, 0.1, 1000.0
        )  # FIXME: hard-coded near and far
        mvp_mtx: Float[Tensor, "B 4 4"] = get_mvp_matrix(c2w, proj_mtx)

        self.mvp_mtx = mvp_mtx
        self.c2w = c2w

        self.camera_positions = camera_positions
        self.light_positions = light_positions
        self.elevation, self.azimuth = elevation, azimuth
        self.elevation_deg, self.azimuth_deg = elevation_deg, azimuth_deg
        self.camera_distances = camera_distances
        self.fovy = fovy

    def __len__(self):
        return self.n_views

    def __getitem__(self, index):
        # return {
        #     "index": index,
        #     "mvp_mtx": self.mvp_mtx[index],
        #     "c2w": self.c2w[index],
        #     "camera_positions": self.camera_positions[index],
        #     "light_positions": self.light_positions[index],
        #     "elevation": self.elevation_deg[index],
        #     "azimuth": self.azimuth_deg[index],
        #     "camera_distances": self.camera_distances[index],
        #     "height": self.cfg.eval_height,
        #     "width": self.cfg.eval_width,
        #     "fovy":self.fovy[index],
        # }
        return {
            # "index": self.selected_views[index] if self.split == "val" else index,
            "index": index,
            # "camera": [Camera_HumanGaussian(c2w=self.c2w[index],FoVy=self.fovy[index],height=self.cfg.eval_height,width=self.cfg.eval_width)],
            "c2w": self.c2w[index],
            "fovy":self.fovy[index],
            "height": self.cfg.eval_height,
            "width": self.cfg.eval_width,
        }        

    def collate(self, batch):
        batch = torch.utils.data.default_collate(batch)
        batch.update({"height": self.cfg.eval_height, "width": self.cfg.eval_width})
        return batch


@register("random-camera-datamodule-new")
class RandomCameraDataModule(pl.LightningDataModule):
    cfg: RandomCameraDataModuleConfig

    def __init__(self, cfg: Optional[Union[dict, DictConfig]] = None) -> None:
        super().__init__()
        self.cfg = parse_structured(RandomCameraDataModuleConfig, cfg)

    def setup(self, stage=None) -> None:
        if stage in [None, "fit"]:
            self.train_dataset = RandomCameraIterableDataset(self.cfg)
        if stage in [None, "fit", "validate"]:
            self.val_dataset = RandomCameraDataset(self.cfg, "val")
        if stage in [None, "test", "predict"]:
            self.test_dataset = RandomCameraDataset(self.cfg, "test")

    def prepare_data(self):
        pass

    def general_loader(self, dataset, batch_size, collate_fn=None) -> DataLoader:
        return DataLoader(
            dataset,
            # very important to disable multi-processing if you want to change self attributes at runtime!
            # (for example setting self.width and self.height in update_step)
            num_workers=0,  # type: ignore
            batch_size=batch_size,
            collate_fn=collate_fn,
        )

    def train_dataloader(self) -> DataLoader:
        return self.general_loader(
            self.train_dataset, batch_size=None, collate_fn=self.train_dataset.collate
        )

    def val_dataloader(self) -> DataLoader:
        return self.general_loader(
            self.val_dataset, batch_size=1, collate_fn=self.val_dataset.collate
        )
        # return self.general_loader(self.train_dataset, batch_size=None, collate_fn=self.train_dataset.collate)

    def test_dataloader(self) -> DataLoader:
        return self.general_loader(
            self.test_dataset, batch_size=1, collate_fn=self.test_dataset.collate
        )

    def predict_dataloader(self) -> DataLoader:
        return self.general_loader(
            self.test_dataset, batch_size=1, collate_fn=self.test_dataset.collate
        )