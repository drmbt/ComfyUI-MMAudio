import os
import torch
import json
import gc
from torchvision.transforms import v2
import torchaudio
from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device

import folder_paths
import comfy.model_management as mm
from comfy.utils import load_torch_file

script_directory = os.path.dirname(os.path.abspath(__file__))

if not "mmaudio" in folder_paths.folder_names_and_paths:
    folder_paths.add_model_folder_path("mmaudio", os.path.join(folder_paths.models_dir, "mmaudio"))


from .mmaudio.eval_utils import (ModelConfig, all_model_cfg, generate,
                                load_video, make_video, setup_eval_logging)
from .mmaudio.model.flow_matching import FlowMatching
from .mmaudio.model.networks import MMAudio, get_my_mmaudio
from .mmaudio.model.utils.features_utils import FeaturesUtils
from .mmaudio.model.sequence_config import (CONFIG_16K, CONFIG_44K, SequenceConfig)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def process_video_tensor(video_tensor: torch.Tensor, duration_sec: float) -> tuple[torch.Tensor, torch.Tensor, float]:
    _CLIP_SIZE = 384
    _CLIP_FPS = 8.0

    _SYNC_SIZE = 224
    _SYNC_FPS = 25.0

    clip_transform = v2.Compose([
        v2.Resize((_CLIP_SIZE, _CLIP_SIZE), interpolation=v2.InterpolationMode.BICUBIC),
        v2.ToPILImage(),
        v2.ToTensor(),
        v2.ConvertImageDtype(torch.float32),
    ])

    sync_transform = v2.Compose([
        v2.Resize(_SYNC_SIZE, interpolation=v2.InterpolationMode.BICUBIC),
        v2.CenterCrop(_SYNC_SIZE),
        v2.ToPILImage(),
        v2.ToTensor(),
        v2.ConvertImageDtype(torch.float32),
        v2.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ])

    # Assuming video_tensor is in the shape (frames, height, width, channels)
    total_frames = video_tensor.shape[0]
    clip_frames_count = int(_CLIP_FPS * duration_sec)
    sync_frames_count = int(_SYNC_FPS * duration_sec)

    if total_frames < clip_frames_count or total_frames < sync_frames_count:
        raise ValueError("Video tensor does not have enough frames for the specified duration.")

    clip_frames = video_tensor[:clip_frames_count]
    sync_frames = video_tensor[:sync_frames_count]

    clip_frames = clip_frames.permute(0, 3, 1, 2)
    sync_frames = sync_frames.permute(0, 3, 1, 2)

    clip_frames = torch.stack([clip_transform(frame) for frame in clip_frames])
    sync_frames = torch.stack([sync_transform(frame) for frame in sync_frames])

    clip_length_sec = clip_frames.shape[0] / _CLIP_FPS
    sync_length_sec = sync_frames.shape[0] / _SYNC_FPS

    if clip_length_sec < duration_sec:
        log.warning(f'Clip video is too short: {clip_length_sec:.2f} < {duration_sec:.2f}')
        log.warning(f'Truncating to {clip_length_sec:.2f} sec')
        duration_sec = clip_length_sec

    if sync_length_sec < duration_sec:
        log.warning(f'Sync video is too short: {sync_length_sec:.2f} < {duration_sec:.2f}')
        log.warning(f'Truncating to {sync_length_sec:.2f} sec')
        duration_sec = sync_length_sec

    clip_frames = clip_frames[:int(_CLIP_FPS * duration_sec)]
    sync_frames = sync_frames[:int(_SYNC_FPS * duration_sec)]

    return clip_frames, sync_frames, duration_sec

#region Model loading
class MMAudioModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mmaudio_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from the 'ComfyUI/models/mmaudio' -folder",}),
            
            "base_precision": (["fp16", "fp32", "bf16"], {"default": "fp16"}),
            },
        }

    RETURN_TYPES = ("MMAUDIO_MODEL",)
    RETURN_NAMES = ("mmaudio_model", )
    FUNCTION = "loadmodel"
    CATEGORY = "MMAudio"

    def loadmodel(self, mmaudio_model, base_precision):
       

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
       
        mm.soft_empty_cache()

        base_dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[base_precision]

        mmaudio_model_path = folder_paths.get_full_path_or_raise("mmaudio", mmaudio_model)
        mmaudio_sd = load_torch_file(mmaudio_model_path, device=offload_device)

        num_heads = 7
        small_model = MMAudio(
                    latent_dim=40,
                    clip_dim=1024,
                    sync_dim=768,
                    text_dim=1024,
                    hidden_dim=64 * num_heads,
                    depth=12,
                    fused_depth=8,
                    num_heads=num_heads,
                    latent_seq_len=345,
                    clip_seq_len=64,
                    sync_seq_len=192
                    ).eval().to(device=device, dtype=base_dtype)
        # load a pretrained model
        # num_heads = 14
        # model = MMAudio(latent_dim=40,
        #            clip_dim=1024,
        #            sync_dim=768,
        #            text_dim=1024,
        #            hidden_dim=64 * num_heads,
        #            depth=21,
        #            fused_depth=14,
        #            num_heads=num_heads,
        #            latent_seq_len=345,
        #            clip_seq_len=64,
        #            sync_seq_len=192,
        #            v2=True
        #            ).eval().to(device=device, dtype=base_dtype)
        model = small_model
        model.load_weights(mmaudio_sd)
        log.info(f'Loaded weights from {mmaudio_model_path}')
        model.seq_cfg = CONFIG_44K
        
       
        return (model,)
    
#region Features Utils

class MMAudioFeatureUtilsLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "vae_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
                "synchformer_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
                "bigvgan_vocoder_model": (folder_paths.get_filename_list("mmaudio"), {"tooltip": "These models are loaded from 'ComfyUI/models/mmaudio'"}),
            },
            "optional": {
                "precision": (["fp16", "fp32", "bf16"],
                    {"default": "bf16"}
                ),
            }
        }

    RETURN_TYPES = ("MMAUDIO_FEATUREUTILS",)
    RETURN_NAMES = ("mmaudio_featureutils", )
    FUNCTION = "loadmodel"
    CATEGORY = "MMAudio"

    def loadmodel(self, vae_model, precision, synchformer_model, bigvgan_vocoder_model):
        
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        vae_path = folder_paths.get_full_path_or_raise("mmaudio", vae_model)
        synchformer_path = folder_paths.get_full_path_or_raise("mmaudio", synchformer_model)
        bigvgan_16k_path = folder_paths.get_full_path_or_raise("mmaudio", bigvgan_vocoder_model)

        feature_utils = FeaturesUtils(tod_vae_ckpt=vae_path,
                                  synchformer_ckpt=synchformer_path,
                                  enable_conditions=True,
                                  mode="44k",
                                  bigvgan_vocoder_ckpt=bigvgan_16k_path).eval().to(device=device, dtype=dtype)

        return (feature_utils,)

#region sampling
class MMAudioSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mmaudio_model": ("MMAUDIO_MODEL",),
                "feature_utils": ("MMAUDIO_FEATUREUTILS",),
                "images": ("IMAGE",),
                "duration": ("FLOAT", {"default": 8, "step": 0.01, "tooltip": "Duration of the audio in seconds"}),
                "steps": ("INT", {"default": 25, "step": 1, "tooltip": "Number of steps to interpolate"}),
                "cfg": ("FLOAT", {"default": 4.5, "step": 0.1, "tooltip": "Strength of the conditioning"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "prompt": ("STRING", {"default": "", "multiline": True} ),
                "negative_prompt": ("STRING", {"default": "", "multiline": True} ),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio", )
    FUNCTION = "sample"
    CATEGORY = "MMAudio"

    def sample(self, images, mmaudio_model, seed, feature_utils, duration, steps, cfg, prompt, negative_prompt):
        device = mm.get_torch_device()
        #clip_frames = images
        rng = torch.Generator(device=device)
        rng.manual_seed(seed)

        seq_cfg = mmaudio_model.seq_cfg

        images = images.to(device=device)
        if images is not None:
            clip_frames, sync_frames, duration = process_video_tensor(images, duration)
            print("clip_frames", clip_frames.shape, "sync_frames", sync_frames.shape, "duration", duration)
            mask_away_clip = False
            if mask_away_clip:
                clip_frames = None
            else:
                clip_frames = clip_frames.unsqueeze(0)
            sync_frames = sync_frames.unsqueeze(0)
        print(clip_frames.shape, sync_frames.shape, duration)

        seq_cfg.duration = duration
        mmaudio_model.update_seq_lengths(seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len)

        scheduler = FlowMatching(min_sigma=0, inference_mode='euler', num_steps=steps)
        
        audios = generate(clip_frames,
                      sync_frames, [prompt],
                      negative_text=[negative_prompt],
                      feature_utils=feature_utils,
                      net=mmaudio_model,
                      fm=scheduler,
                      rng=rng,
                      cfg_strength=cfg)
        waveform = audios.float().cpu()
        #torchaudio.save("test.wav", waveform, 44100)
        audio = {
            "waveform": waveform,
            "sample_rate": 44100
        }

        return (audio,)

class TorchaudioToVHS:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "audio": ("TORCHAUDIOTENSOR",),
            "sample_rate": ("INT", {"default": 44100, "min": 0, "max": 48000}),
             },
        }

    RETURN_TYPES = ("AUDIO", "INT",)
    RETURN_NAMES = ("audio", "audio_dur",)
    FUNCTION = "process"
    CATEGORY = "MMAudio"

    def process(self, audio, sample_rate):

        # Save generated audio to a WAV file
        audio_path = os.path.join(script_directory, "temp", "gen_audio.wav")
        
        torchaudio.save(audio_path, audio, sample_rate)
        audio_info = torchaudio.info(audio_path)
        
        audio_dur = audio_info.num_frames / audio_info.sample_rate
 
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            imageio_ffmpeg_path = get_ffmpeg_exe()
        except:
            print("Failed to import imageio_ffmpeg")
        args = [imageio_ffmpeg_path, "-v", "error", "-i", audio_path]
        try:
            import subprocess
            res =  subprocess.run(args + ["-f", "wav", "-"],
                                stdout=subprocess.PIPE, check=True).stdout
        except:
            print("Failed to run ffmpeg")
  
        audio_lambda = lambda: res

        # Return the new audio_lambda
        return (audio_lambda, audio_dur,)
        
NODE_CLASS_MAPPINGS = {
    "MMAudioModelLoader": MMAudioModelLoader,
    "MMAudioFeatureUtilsLoader": MMAudioFeatureUtilsLoader,
    "MMAudioSampler": MMAudioSampler,
    "TorchaudioToVHS": TorchaudioToVHS,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MMAudioModelLoader": "MMAudio ModelLoader",
    "MMAudioFeatureUtilsLoader": "MMAudio FeatureUtilsLoader",
    "MMAudioSampler": "MMAudio Sampler",
    "TorchaudioToVHS": "Torchaudio to VHS",
    }
