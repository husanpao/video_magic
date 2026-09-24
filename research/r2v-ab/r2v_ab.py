#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R2V 身份锁定 A/B 实验驱动

目的：判定「FL2VA 权重 + 官方 MiniMaxH3ReferenceToVideo 节点」的角色身份锁定强度，
      以决定是否需要下载 21GB 官方 Ref2VA 权重。

方法（单变量对照）：
  - 同一提示词、同一种子，只改「参考图 / ref_image_size」一个变量
  - 对照臂 ctrl（换一个人物参考图）用于验证参考图是否真的被生效
    （若 ctrl 与 match 输出无差异 → 参考根本没被用上，实验无效）

工作流结构完全复刻 ~/comfyui/h3-test-run.py 的 R2V 分支（已在本机跑通过 R2VTEST）。
本脚本只读 ComfyUI HTTP API，不修改 ComfyUI 或用户任何文件。
"""
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
MODEL = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
LORA8 = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_V = "minimax_h3_video_vae_fp16.safetensors"
VAE_A = "minimax_h3_audio_vae_fp32.safetensors"

W, H = 864, 480
LENGTH = 124          # 17*7+5 = 5.17s @24fps
STEPS = 8
SEED = 1688


def post(path, payload):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get(path, timeout=30):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.load(r)


def build(prompt, prefix, ref_images=None, ref_image_size="match"):
    """复刻 h3-test-run.py 的 R2V 工作流；ref_images 非空走 MiniMaxH3ReferenceToVideo。"""
    g = {
        "1": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_V}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_A}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "minimax", "device": "default"}},
        "4": {"class_type": "UNETLoader",
              "inputs": {"unet_name": MODEL, "weight_dtype": "default"}},
        "5": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"lora_name": LORA8, "strength_model": 1.0, "model": ["4", 0]}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": SEED}},
        "13": {"class_type": "VHS_VideoCombine",
               "inputs": {"frame_rate": 24, "loop_count": 0, "filename_prefix": prefix,
                          "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19,
                          "save_metadata": True, "trim_to_audio": False,
                          "pingpong": False, "save_output": True,
                          "images": ["12", 0], "audio": ["12", 1]}},
    }
    if not ref_images:
        # 无参考：走 T8 条件节点（基线臂）
        g["7"] = {"class_type": "MiniMaxH3AudioConditioningT8",
                  "inputs": {"prompt": prompt, "width": W, "height": H, "length": LENGTH,
                             "task_type": "T2VA", "audio_mode": "native",
                             "audio_denoise_strength": 1.0, "add_source_as_reference": False,
                             "prompt_primary_audio_ordinal": 0, "strict_prompt_tags": True,
                             "ref_image_size": ref_image_size,
                             "reference_video_policy": "official_2_to_15s",
                             "clip": ["3", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]}}
        g["8"] = {"class_type": "MiniMaxH3DualClockSamplerT8",
                  "inputs": {"steps": STEPS, "shift_video": 12.0, "shift_audio": 3.0,
                             "model": ["5", 0], "av_latent": ["7", 1]}}
        g["10"] = {"class_type": "BasicGuider",
                   "inputs": {"model": ["8", 0], "conditioning": ["7", 0]}}
        g["11"] = {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 1],
                              "sigmas": ["8", 2], "latent_image": ["7", 1]}}
        g["12"] = {"class_type": "MiniMaxH3AVDecodeT8",
                   "inputs": {"av_latent": ["11", 0], "video_vae": ["1", 0],
                              "audio_vae": ["2", 0]}}
        return g

    # R2V：官方参考图节点
    r2v = {"clip": ["3", 0], "prompt": prompt, "width": W, "height": H, "length": LENGTH,
           "vae": ["1", 0], "audio_vae": ["2", 0], "ref_image_size": ref_image_size}
    for i, name in enumerate(ref_images):
        nid = "img%d" % i
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name, "upload": "image"}}
        r2v["ref_images.ref_image_%d" % i] = [nid, 0]
    g["7"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": r2v}
    g["8"] = {"class_type": "MiniMaxH3DualClockSamplerT8",
              "inputs": {"steps": STEPS, "shift_video": 12.0, "shift_audio": 3.0,
                         "model": ["5", 0], "av_latent": ["7", 1]}}
    g["10"] = {"class_type": "BasicGuider",
               "inputs": {"model": ["8", 0], "conditioning": ["7", 0]}}
    g["11"] = {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 1],
                          "sigmas": ["8", 2], "latent_image": ["7", 1]}}
    g["12"] = {"class_type": "MiniMaxH3AVDecodeT8",
               "inputs": {"av_latent": ["11", 0], "video_vae": ["1", 0],
                          "audio_vae": ["2", 0]}}
    return g


def run_one(label, prompt, prefix, refs=None, ref_size="match", timeout=1800):
    g = build(prompt, prefix, ref_images=refs, ref_image_size=ref_size)
    t0 = time.time()
    try:
        res = post("/prompt", {"prompt": g})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("  ❌ %-8s 提交失败 HTTP %s: %s" % (label, e.code, body[:400]))
        return None
    pid = res.get("prompt_id")
    if not pid:
        print("  ❌ %-8s 无 prompt_id: %s" % (label, json.dumps(res)[:300]))
        return None
    print("  ▶ %-8s prompt_id=%s" % (label, pid))
    while time.time() - t0 < timeout:
        time.sleep(10)
        try:
            h = get("/history/%s" % pid, timeout=30)
        except Exception:
            continue
        if pid not in h:
            continue
        entry = h[pid]
        status = entry.get("status", {})
        outs = entry.get("outputs", {})
        files = []
        for node in outs.values():
            for v in node.values():
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and it.get("filename"):
                            files.append(it["filename"])
        el = time.time() - t0
        if status.get("status_str") == "error":
            msgs = status.get("messages", [])
            print("  ❌ %-8s 执行出错 (%.0fs): %s" % (label, el, json.dumps(msgs)[:400]))
            return None
        print("  ✅ %-8s 完成 %.1fs → %s" % (label, el, files))
        return files
    print("  ⏱ %-8s 超时" % label)
    return None


# ── 六段式 Ref2VA 提示词（NiliX 实际驱动的格式）────────────────────────────────
COMMON_DEF = (
    "subject_definitions: <Subject 1> = 王梅, a Chinese rural woman in her late twenties, "
    "faded indigo coarse cotton padded jacket with a patch on the left elbow, black hair in a "
    "thick braid, resolute eyes, weathered tan skin. <Picture 1> supplies the identity and "
    "appearance of <Subject 1>.\n"
    "summary: reference generation\n"
    "retention_analysis: <Subject 1> must be fully_preserved in facial features, hairstyle, "
    "and clothing across the whole clip; do not alter age, do not change the jacket colour, "
    "do not restyle the braid.\n"
)

SHOTS = {
    "shot1": (
        COMMON_DEF +
        "detailed_description: [Shot 1] Medium close-up of <Subject 1> standing still facing "
        "the camera directly in a dim mud-brick farmhouse interior, a paper-paned window behind "
        "her. She looks steadily at the camera with a determined expression, lips pressed "
        "together. Muted desaturated earthy tones, slight film grain. The camera slowly pushes in "
        "with small amplitude at slow speed.\n"
    ),

    "shot2": (
        COMMON_DEF +
        "detailed_description: [Shot 1] Wide shot of <Subject 1> walking away from the camera up "
        "a scrub-covered hillside ridge in rural North China, seen from behind and slightly to "
        "the side, pushing aside tall dry weeds with her hands. Bare brown hills and an overcast "
        "sky beyond. Muted desaturated earthy tones, slight film grain. The camera tracks left "
        "at slow speed.\n"
    ),
    "shot3": (
        COMMON_DEF +
        "detailed_description: [Shot 1] Medium shot in profile of <Subject 1> crouching beside a "
        "low wooden table in a humble earthen room, her face turned three-quarters away from the "
        "camera as she folds a stack of old paper banknotes with both hands. Warm dim lamplight "
        "from the left. Muted desaturated earthy tones, slight film grain. The camera arcs around "
        "the subject at slow speed.\n"
    ),
    # 中立提示词：**不在文字里描述任何外貌**，外貌完全交由参考图决定。
    # 用于把「参考图对身份的实际影响力」从「文字描述的主导力」中剥离出来。
    "neutral": (
        "subject_definitions: <Subject 1> = the person shown in <Picture 1>. "
        "<Picture 1> supplies the identity and appearance of <Subject 1>.\n"
        "summary: reference generation\n"
        "retention_analysis: <Subject 1> must be fully_preserved in facial features, hairstyle, "
        "and clothing; do not alter age or gender; do not change the clothing colour.\n"
        "detailed_description: [Shot 1] Medium close-up of <Subject 1> standing still facing the "
        "camera directly in a dim mud-brick farmhouse interior, a paper-paned window behind them. "
        "Muted desaturated earthy tones, slight film grain. The camera slowly pushes in with "
        "small amplitude at slow speed.\n"
    ),
    # 中立 + 换场景/景别 → 测「同一参考图跨镜头身份是否稳定」
    "neutral2": (
        "subject_definitions: <Subject 1> = the person shown in <Picture 1>. "
        "<Picture 1> supplies the identity and appearance of <Subject 1>.\n"
        "summary: reference generation\n"
        "retention_analysis: <Subject 1> must be fully_preserved in facial features, hairstyle, "
        "and clothing; do not alter age or gender.\n"
        "detailed_description: [Shot 1] Wide shot of <Subject 1> walking away from the camera up "
        "a scrub-covered hillside ridge in rural North China, seen from behind and slightly to "
        "the side, pushing aside tall dry weeds with both hands. Bare brown hills and an overcast "
        "sky beyond. Muted desaturated earthy tones, slight film grain. The camera tracks left at "
        "slow speed.\n"
    ),
    "neutral3": (
        "subject_definitions: <Subject 1> = the person shown in <Picture 1>. "
        "<Picture 1> supplies the identity and appearance of <Subject 1>.\n"
        "summary: reference generation\n"
        "retention_analysis: <Subject 1> must be fully_preserved in facial features, hairstyle, "
        "and clothing; do not alter age or gender.\n"
        "detailed_description: [Shot 1] Medium close-up in three-quarter view of <Subject 1> "
        "sitting at a low wooden table in a humble earthen room, looking down at a stack of old "
        "paper banknotes held in both hands. Warm dim lamplight from the left. Muted desaturated "
        "earthy tones, slight film grain. The camera arcs around the subject at slow speed.\n"
    ),
}

TAIL = "overall_soundscape: wind, faint footsteps, cloth rustle\nnon_diegetic_music: none\n"

ARMS = [
    # label,      shot,    refs,                    ref_image_size
    ("base",      "shot1",  None,                    "match"),
    ("match",     "shot1",  ["char_wangmei.png"],    "match"),
    ("max",       "shot1",  ["char_wangmei.png"],    "max"),
    ("ctrl",      "shot1",  ["char_liufugui.png"],   "match"),
    ("b_shot2",   "shot2",  ["char_wangmei.png"],    "match"),
    ("b_shot3",   "shot3",  ["char_wangmei.png"],    "match"),
    # 中立提示词：文字不描述外貌，纯靠参考图 → 判定参考图对身份的真实影响力
    ("n_wm",      "neutral", ["char_wangmei.png"],   "match"),
    ("n_lf",      "neutral", ["char_liufugui.png"],  "match"),
    # 同一参考图跨镜头稳定性 + ref_image_size 影响
    ("n_s2",      "neutral2", ["char_wangmei.png"],  "match"),
    ("n_s3",      "neutral3", ["char_wangmei.png"],  "match"),
    ("n_max",     "neutral",  ["char_wangmei.png"],  "max"),
]

if __name__ == "__main__":
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    print("=" * 78)
    print("R2V 身份锁定 A/B  |  FL2VA 剪枝版 + MiniMaxH3ReferenceToVideo  |  %dx%d  %d帧  %d步  seed=%d"
          % (W, H, LENGTH, STEPS, SEED))
    print("=" * 78)
    results = {}
    for label, shot, refs, rsize in ARMS:
        if only and label not in only:
            continue
        prompt = SHOTS[shot] + TAIL
        results[label] = run_one(label, prompt, "R2VAB_%s" % label,
                                 refs=refs, ref_size=rsize)
    print("\n===== 汇总 =====")
    for k, v in results.items():
        print("  %-9s → %s" % (k, v))
