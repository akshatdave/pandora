from models.frameworks import get_model
from models.base import get_optimizer, get_scheduler
from models.frameworks.volsdf import spec_net_to_lin
from utils import rend_util, train_util, mesh_util, io_util
from utils.dist_util import get_local_rank, init_env, is_master, get_rank, get_world_size
from utils.print_fn import log
from utils.logger import Logger
from utils.checkpoints import CheckpointIO
from dataio import get_data

import imageio

import os
import sys
import time
import functools
from tqdm import tqdm

import torch
import torch.nn.functional as F
import torch.distributed as dist
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

from src.utils import linear_rgb_to_srgb, linear_rgb_to_srgb_np

from tools.render_view import write_env_map_tb

def main_function(args):

    init_env(args)
    
    #----------------------------
    #-------- shortcuts ---------
    rank = get_rank()
    local_rank = get_local_rank()
    world_size = get_world_size()
    i_backup = int(args.training.i_backup // world_size) if args.training.i_backup > 0 else -1
    i_val = int(args.training.i_val // world_size) if args.training.i_val > 0 else -1
    i_val_mesh = int(args.training.i_val_mesh // world_size) if args.training.i_val_mesh > 0 else -1
    special_i_val_mesh = [int(i // world_size) for i in [3000, 5000, 7000]]
    exp_dir = args.training.exp_dir
    mesh_dir = os.path.join(exp_dir, 'meshes')
    
    device = torch.device('cuda', local_rank)


    # logger
    logger = Logger(
        log_dir=exp_dir,
        img_dir=os.path.join(exp_dir, 'imgs'),
        monitoring=args.training.get('monitoring', 'tensorboard'),
        monitoring_dir=os.path.join(exp_dir, 'events'),
        rank=rank, is_master=is_master(), multi_process_logging=(world_size > 1))

    log.info("=> Experiments dir: {}".format(exp_dir))

    if is_master():
        # backup codes
        io_util.backup(os.path.join(exp_dir, 'backup'))

        # save configs
        io_util.save_config(args, os.path.join(exp_dir, 'config.yaml'))
    
    dataset, val_dataset = get_data(args, return_val=True, val_downscale=args.data.get('val_downscale', 4.0))
    bs = args.data.get('batch_size', None)
    if args.ddp:
        train_sampler = DistributedSampler(dataset)
        dataloader = torch.utils.data.DataLoader(dataset, sampler=train_sampler, batch_size=bs)
        val_sampler = DistributedSampler(val_dataset)
        valloader = torch.utils.data.DataLoader(val_dataset, sampler=val_sampler, batch_size=bs)
    else:
        dataloader = DataLoader(dataset,
            batch_size=bs,
            shuffle=True,
            pin_memory=args.data.get('pin_memory', False))
        valloader = DataLoader(val_dataset,
            batch_size=1,
            shuffle=True)
    
    # Create model
    model, trainer, render_kwargs_train, render_kwargs_test, volume_render_fn = get_model(args)
    model.to(device)
    log.info(model)
    log.info("=> Nerf params: " + str(train_util.count_trainable_parameters(model)))

    render_kwargs_train['H'] = dataset.H
    render_kwargs_train['W'] = dataset.W
    render_kwargs_test['H'] = val_dataset.H
    render_kwargs_test['W'] = val_dataset.W

    # build optimizer
    optimizer = get_optimizer(args, model)

    # checkpoints
    checkpoint_io = CheckpointIO(checkpoint_dir=os.path.join(exp_dir, 'ckpts'), allow_mkdir=is_master())
    if world_size > 1:
        dist.barrier()
    # Register modules to checkpoint
    checkpoint_io.register_modules(
        model=model,
        optimizer=optimizer,
    )

    # Load checkpoints
    load_dict = checkpoint_io.load_file(
        args.training.ckpt_file,
        ignore_keys=args.training.ckpt_ignore_keys,
        only_use_keys=args.training.ckpt_only_use_keys,
        map_location=device)

    logger.load_stats('stats.p')    # this will be used for plotting
    it = load_dict.get('global_step', 0)
    epoch_idx = load_dict.get('epoch_idx', 0)

    # pretrain if needed. must be after load state_dict, since needs 'is_pretrained' variable to be loaded.
    #---------------------------------------------
    #-------- init perparation only done in master
    #---------------------------------------------
    if is_master():
        pretrain_config = {'logger': logger}
        if 'lr_pretrain' in args.training:
            pretrain_config['lr'] = args.training.lr_pretrain
            if(model.implicit_surface.pretrain_hook(pretrain_config)):
                checkpoint_io.save(filename='latest.pt'.format(it), global_step=it, epoch_idx=epoch_idx)

    # Parallel training
    if args.ddp:
        trainer = DDP(trainer, device_ids=args.device_ids, output_device=local_rank, find_unused_parameters=False)

    # build scheduler
    scheduler = get_scheduler(args, optimizer, last_epoch=it-1)
    t0 = time.time()
    log.info('=> Start training..., it={}, lr={}, in {}'.format(it, optimizer.param_groups[0]['lr'], exp_dir))
    end = (it >= args.training.num_iters)
    with tqdm(range(args.training.num_iters), disable=not is_master()) as pbar:
        if is_master():
            pbar.update(it)
        while it <= args.training.num_iters and not end:
            try:
                if args.ddp:
                    train_sampler.set_epoch(epoch_idx)
                for (indices, model_input, ground_truth) in dataloader:
                    int_it = int(it // world_size)
                    #-------------------
                    # validate
                    #-------------------
                    if i_val > 0 and int_it % i_val == 0:
                        with torch.no_grad():
                            (val_ind, val_in, val_gt) = next(iter(valloader))
                            
                            intrinsics = val_in["intrinsics"].to(device)
                            c2w = val_in['c2w'].to(device)
                            
                            # N_rays=-1 for rendering full image
                            rays_o, rays_d, select_inds = rend_util.get_rays(
                                c2w, intrinsics, render_kwargs_test['H'], render_kwargs_test['W'], N_rays=-1)
                            if not args.data.gt_type == 'stokes':
                                target_rgb = val_gt['rgb'].to(device)                      
                            # For diffuse and specular, disable specular rendering 
                            if not args.model.only_diffuse:
                                if it < args.training.num_no_s1_s2:
                                    render_kwargs_test['only_diffuse'] = True
                                else:
                                    render_kwargs_test['only_diffuse'] = False
                            rgb, depth_v, ret = volume_render_fn(rays_o, rays_d, calc_normal=True, detailed_output=True, **render_kwargs_test)

                            to_img = functools.partial(
                                rend_util.lin2img, 
                                H=render_kwargs_test['H'], W=render_kwargs_test['W'],
                                batched=render_kwargs_test['batched'])

                            if args.data.space == 'linear':
                                to_space = lambda x:linear_rgb_to_srgb(x)
                            elif args.data.space == 'srgb':
                                to_space = lambda x:x

                            if not args.data.gt_type == 'stokes':
                                logger.add_imgs(to_img(to_space(target_rgb)), 'val/gt_rgb', it)
                            logger.add_imgs(to_img(to_space(rgb)), 'val/predicted_rgb', it)
                            logger.add_imgs(to_img((depth_v/(depth_v.max()+1e-10)).unsqueeze(-1)), 'val/pred_depth_volume', it)
                            logger.add_imgs(to_img(ret['mask_volume'].unsqueeze(-1)), 'val/pred_mask_volume', it)
                            if 'depth_surface' in ret:
                                logger.add_imgs(to_img((ret['depth_surface']/ret['depth_surface'].max()).unsqueeze(-1)), 'val/pred_depth_surface', it)
                            if 'mask_surface' in ret:
                                logger.add_imgs(to_img(ret['mask_surface'].unsqueeze(-1).float()), 'val/predicted_mask', it)
                            if hasattr(trainer, 'val'):
                                trainer.val(logger, ret, to_img, it, render_kwargs_test)
                            
                            logger.add_imgs(to_img(ret['normals_volume']/2.+0.5), 'val/predicted_normals', it)
                            if args.model.polarized:
                                if not args.data.gt_type == 'stokes':
                                    target_normal = val_gt['normal'].to(device)
                                    logger.add_imgs(to_img(target_normal/2.+0.5), 'val/gt_normals', it)
                            if (not args.model.only_diffuse) and (it>args.training.num_no_s1_s2):
                                if not args.data.gt_type == 'stokes':
                                    target_specular = val_gt['specular'].to(device)
                                    logger.add_imgs(to_img(to_space(target_specular)), 'val/gt_specular', it)
                                logger.add_imgs(to_img(to_space(ret['spec_map'])),'val/predicted_specular',it)

                                if not (args.model.use_env_mlp in ['no_envmap_MLP', 'mask_no_envmap_MLP']):
                                    env_map_dir = "val/env_map"
                                    full_envmap_path = "%s/%s" % (logger.img_dir, env_map_dir)
                                    # if not(os.path.exists(full_envmap_path)):
                                    #     os.makedirs(full_envmap_path)
                                    logger.add_imgs(write_env_map_tb(model.specular_net, "%s/%08d"%(full_envmap_path,it), device,
                                                    fres_in_mlp=args.model.env_mlp_type=='fres_input',
                                                    use_roughness=args.model.use_env_mlp in ['rough_envmap_MLP','rough_mask_envmap_MLP']), 
                                                    env_map_dir, it)


                                if 'rough_map' in ret.keys():
                                    rough_dir = 'val/roughness'
                                    rough_map = ret['rough_map'].unsqueeze(-1)
                                    logger.add_imgs(to_img((rough_map)),rough_dir,it)
                                    full_roughmap_path = "%s/%s/%08d" % (logger.img_dir, rough_dir, it)
                                    rough_map_torch = torch.reshape(rough_map, (render_kwargs_test['H'], render_kwargs_test['W']))
                                    imageio.imwrite(f"{full_roughmap_path}.exr", rough_map_torch.cpu().detach().numpy())

                            # Plot polarimetric cues
                            if args.model.polarized:
                                from src.polarization import cues_from_stokes, colorize_cues, stokes_from_normal_rad
                                # Predicted
                                pred_stokes = torch.stack([ret['s0'],
                                                           ret['s1'],
                                                           ret['s2']], -1).cpu()
                                pred_cues = colorize_cues(cues_from_stokes(pred_stokes),
                                                        gamma_s0=(args.data.space=='linear'))
                                for cue_name, cue_val in pred_cues.items():
                                    logger.add_imgs(to_img(cue_val),f'val/cues_predicted_{cue_name}',it)
                                if 'spec_fac0' in ret:
                                    logger.add_imgs(to_img(ret['spec_fac0']),f'val/pred_spec_fac0',it)
                                if 'fres_out' in ret:
                                    logger.add_imgs(to_img(ret['fres_out']),f'val/pred_fres_out',it)
                                if 'fres_diff' in ret:
                                    logger.add_imgs(to_img(ret['fres_diff']),f'val/pred_fres_diff',it)
                                if 'mask_map' in ret:
                                    logger.add_imgs(to_img(ret['mask_map'].unsqueeze(-1)),f'val/pred_mask_map',it)
                                if args.data.gt_type == 'normal':
                                    # GT
                                    target_normal = val_gt['normal'].to(device)
                                    if args.model.only_diffuse:
                                        # [B, N_rays,3, 3]
                                        target_stokes = stokes_from_normal_rad(rays_o, rays_d, target_normal, 
                                                        target_rgb, train_mode=True).cpu()
                                    else:
                                        target_specular = val_gt['specular'].to(device)
                                        target_stokes = stokes_from_normal_rad(rays_o, rays_d, target_normal, 
                                                        target_rgb, spec_rads=target_specular, 
                                                        train_mode=True).cpu()
                                elif args.data.gt_type == 'stokes':
                                    target_stokes = torch.stack([val_gt['s0'].to(device), 
                                                                 val_gt['s1'].to(device),
                                                                 val_gt['s2'].to(device)], -1).cpu()
                                else:
                                    raise Exception(f'Invalid data gt_type {args.data.gt_type}. Options: stokes, normal')
                                target_cues = colorize_cues(cues_from_stokes(target_stokes),
                                                            gamma_s0=(args.data.space=='linear'))
                                for cue_name, cue_val in target_cues.items():
                                    logger.add_imgs(to_img(cue_val),f'val/cues_gt_{cue_name}',it)

                            # Plot masks
                            if "object_mask" in val_in:
                                logger.add_imgs(to_img((val_in["object_mask"]+0.).to(device).unsqueeze(-1)),f'val/gt_object_mask',it)
                            if "horizon_mask" in val_in:
                                logger.add_imgs(to_img((val_in["horizon_mask"]+0.).to(device).unsqueeze(-1)),f'val/gt_horizon_mask',it)
                    
                    #-------------------
                    # validate mesh
                    #-------------------
                    if is_master():
                        # NOTE: not validating mesh before 3k, as some of the instances of DTU for NeuS training will have no large enough mesh at the beginning.
                        if i_val_mesh > 0 and (int_it % i_val_mesh == 0 or int_it in special_i_val_mesh) and it != 0:
                            with torch.no_grad():
                                io_util.cond_mkdir(mesh_dir)
                                mesh_util.extract_mesh(
                                    model.implicit_surface, 
                                    filepath=os.path.join(mesh_dir, '{:08d}.ply'.format(it)),
                                    volume_size=args.data.get('volume_size', 2.0),
                                    show_progress=is_master())
                                print("Done saving ply!")

                    if it >= args.training.num_iters:
                        end = True
                        break
                    
                    #-------------------
                    # train
                    #-------------------
                    start_time = time.time()
                    ret = trainer.forward(args, indices, model_input, ground_truth, render_kwargs_train, it)
                    
                    losses = ret['losses']
                    extras = ret['extras']

                    for k, v in losses.items():
                        # log.info("{}:{} - > {}".format(k, v.shape, v.mean().shape))
                        losses[k] = torch.mean(v)
                    
                    optimizer.zero_grad()
                    losses['total'].backward()
                    # Clip grad norms
                    train_util.clip_grad_norm(args.training.grad_norm_max,
                                              model=model)
                    # NOTE: check grad before optimizer.step()
                    if True:
                        grad_norms = train_util.calc_grad_norm(model=model)
                    optimizer.step()
                    scheduler.step(it)  # NOTE: important! when world_size is not 1

                    #-------------------
                    # logging
                    #-------------------
                    # done every i_save seconds
                    if (args.training.i_save > 0) and (time.time() - t0 > args.training.i_save):
                        if is_master():
                            checkpoint_io.save(filename='latest.pt', global_step=it, epoch_idx=epoch_idx)
                        # this will be used for plotting
                        logger.save_stats('stats.p')
                        t0 = time.time()
                    
                    if is_master():
                        #----------------------------------------------------------------------------
                        #------------------- things only done in master -----------------------------
                        #----------------------------------------------------------------------------
                        pbar.set_postfix(lr=optimizer.param_groups[0]['lr'], loss_total=losses['total'].item(), loss_img=losses['loss_img'].item())

                        if i_backup > 0 and int_it % i_backup == 0 and it > 0:
                            checkpoint_io.save(filename='{:08d}.pt'.format(it), global_step=it, epoch_idx=epoch_idx)

                    #----------------------------------------------------------------------------
                    #------------------- things done in every child process ---------------------------
                    #----------------------------------------------------------------------------

                    #-------------------
                    # log grads and learning rate
                    for k, v in grad_norms.items():
                        logger.add('grad', k, v, it)
                    logger.add('learning rates', 'whole', optimizer.param_groups[0]['lr'], it)

                    #-------------------
                    # log losses
                    for k, v in losses.items():
                        logger.add('losses', k, v.data.cpu().numpy().item(), it)
                    
                    #-------------------
                    # log extras
                    names = ["radiance", "alpha", "implicit_surface", "implicit_nablas_norm", "sigma_out", "radiance_out",
                             "d_vals","fres_out","spec_s0","diff_s0"]
                    for n in names:
                        p = "whole"
                        # key = "raw.{}".format(n)
                        key = n
                        if key in extras:
                            logger.add("extras_{}".format(n), "{}.mean".format(p), extras[key].mean().data.cpu().numpy().item(), it)
                            logger.add("extras_{}".format(n), "{}.min".format(p), extras[key].min().data.cpu().numpy().item(), it)
                            logger.add("extras_{}".format(n), "{}.max".format(p), extras[key].max().data.cpu().numpy().item(), it)
                            logger.add("extras_{}".format(n), "{}.norm".format(p), extras[key].norm().data.cpu().numpy().item(), it)
                    if 'scalars' in extras:
                        for k, v in extras['scalars'].items():
                            logger.add('scalars', k, v.mean(), it)                           

                    #---------------------
                    # end of one iteration
                    end_time = time.time()
                    log.debug("=> One iteration time is {:.2f}".format(end_time - start_time))
                    
                    it += world_size
                    if is_master():
                        pbar.update(world_size)
                #---------------------
                # end of one epoch
                epoch_idx += 1

            except KeyboardInterrupt:
                if is_master():
                    checkpoint_io.save(filename='latest.pt'.format(it), global_step=it, epoch_idx=epoch_idx)
                    # this will be used for plotting
                logger.save_stats('stats.p')
                sys.exit()

    if is_master():
        checkpoint_io.save(filename='final_{:08d}.pt'.format(it), global_step=it, epoch_idx=epoch_idx)
        logger.save_stats('stats.p')
        log.info("Everything done.")

if __name__ == "__main__":
    # Arguments
    parser = io_util.create_args_parser()
    parser.add_argument("--ddp", action='store_true', help='whether to use DDP to train.')
    parser.add_argument("--port", type=int, default=None, help='master port for multi processing. (if used)')
    args, unknown = parser.parse_known_args()
    config = io_util.load_config(args, unknown)
    main_function(config)