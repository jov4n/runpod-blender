"""
Blender headless plate renderer.
Usage:
  blender --background plate2.blend --python render_plate.py -- --sku D9820 --image design.jpg --output output.mp4
  blender --background plate2.blend --python render_plate.py -- --sku D9820 --image design.jpg --output output.gif
"""
import bpy
import sys
import os
import argparse
import shutil
import subprocess

# ---------------------------------------------------------------------------
# Argument parsing (everything after "--")
# ---------------------------------------------------------------------------
argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = []

parser = argparse.ArgumentParser(description="Render a plate with a custom design image")
parser.add_argument("--sku", required=True, help="Product SKU (D9820, D9609, D9727)")
parser.add_argument("--image", required=True, help="Path to the design image file")
parser.add_argument("--output", default=None, help="Output path (e.g. output.mp4 or output.gif)")
parser.add_argument("--resolution", type=int, default=540, help="Square resolution (default 540; use 1080 with EEVEE/GPU)")
parser.add_argument("--fps", type=int, default=12, help="Frames per second (default 12 for fast CPU; 24 for EEVEE)")
parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds (default 5)")
parser.add_argument("--samples", type=int, default=8, help="Render samples (default 8 — denoiser cleans up the rest)")
parser.add_argument("--engine", default="cycles", choices=["cycles", "eevee"], help="Render engine (default: cycles — works headless; eevee needs GPU)")
parser.add_argument("--quality", type=int, default=80, help="FFMPEG output quality 0-100 (default 80)")
args = parser.parse_args(argv)

# ---------------------------------------------------------------------------
# SKU → Collection + material texture mapping
# ---------------------------------------------------------------------------
SKU_MAP = {
    "D9820": {
        "collection": "P1",
        "materials": {
            "p1": ["Image Texture", "Image Texture.001"],
        },
    },
    "D9609": {
        "collection": "P2",
        "materials": {
            "p3": ["Image Texture"],
        },
    },
    "D9727": {
        "collection": "P3",
        "materials": {
            # Match by image name/path (plate2 uses "PLATTER - Template.jpg"), not only node name
            "P2": {"image_match": ["PLATTER - Template", "Template.jpg"]},
        },
    },
}

PLATE_COLLECTIONS = ["P1", "P2", "P3"]

sku = args.sku.upper()
if sku not in SKU_MAP:
    print(f"ERROR: Unknown SKU '{sku}'. Valid: {list(SKU_MAP.keys())}")
    sys.exit(1)

image_path = os.path.abspath(args.image)
if not os.path.isfile(image_path):
    print(f"ERROR: Image not found: {image_path}")
    sys.exit(1)

config = SKU_MAP[sku]
target_collection = config["collection"]

# Determine output path
ext = ".mp4"
wants_gif = False
if args.output:
    out_path = os.path.abspath(args.output)
    if out_path.lower().endswith(".gif"):
        wants_gif = True
        mp4_path = out_path.rsplit(".", 1)[0] + ".mp4"
    else:
        mp4_path = out_path
else:
    mp4_path = os.path.join(os.path.dirname(image_path), f"{sku}_render.mp4")
    out_path = mp4_path

os.makedirs(os.path.dirname(mp4_path), exist_ok=True)

print(f"=== Plate Renderer ===")
print(f"  SKU:        {sku}")
print(f"  Collection: {target_collection}")
print(f"  Image:      {image_path}")
print(f"  Output:     {out_path}")
print()

# ---------------------------------------------------------------------------
# 1. Show only the target plate collection (+ shared Collection)
# ---------------------------------------------------------------------------
scene = bpy.context.scene
view_layer = bpy.context.view_layer

def set_collection_visibility(layer_collection, target_name, plate_collections):
    """Recursively set collection visibility: hide plate collections that aren't the target."""
    for child in layer_collection.children:
        if child.name in plate_collections:
            exclude = child.name != target_name
            child.exclude = exclude
            coll = bpy.data.collections.get(child.name)
            if coll:
                coll.hide_render = exclude
            print(f"  Collection '{child.name}': {'HIDDEN' if exclude else 'VISIBLE'}")
        set_collection_visibility(child, target_name, plate_collections)

print("Setting collection visibility...")
set_collection_visibility(view_layer.layer_collection, target_collection, PLATE_COLLECTIONS)

# ---------------------------------------------------------------------------
# 2. Load the new image and replace textures
# ---------------------------------------------------------------------------
print(f"\nLoading design image: {image_path}")
new_image = bpy.data.images.load(image_path, check_existing=False)
new_image.colorspace_settings.name = "sRGB"

def _replace_textures_in_material(mat_name, spec):
    """spec is either a list of node names, or a dict with optional keys:
    nodes: [str, ...]   — explicit Image Texture node names
    image_match: [str, ...] — replace any TEX_IMAGE whose image name/path contains a substring (case-insensitive)
    """
    mat = bpy.data.materials.get(mat_name)
    if not mat or not mat.node_tree:
        print(f"  WARNING: Material '{mat_name}' not found or has no nodes")
        return

    nt = mat.node_tree.nodes

    if isinstance(spec, list):
        node_names = spec
        image_substrings = []
    else:
        node_names = spec.get("nodes") or []
        image_substrings = spec.get("image_match") or spec.get("image_contains") or []

    replaced_nodes = set()

    for node_name in node_names:
        node = nt.get(node_name)
        if node and node.type == "TEX_IMAGE":
            old_name = node.image.name if node.image else "None"
            node.image = new_image
            replaced_nodes.add(node.name)
            print(f"  Material '{mat_name}' / Node '{node_name}': {old_name} -> {new_image.name}")
        else:
            print(f"  WARNING: Node '{node_name}' not found in material '{mat_name}'")

    if image_substrings:
        subs_lower = [s.lower() for s in image_substrings]
        matched_by_substring = 0
        for node in nt:
            if node.type != "TEX_IMAGE" or not node.image or node.name in replaced_nodes:
                continue
            img = node.image
            name_l = (img.name or "").lower()
            fp = bpy.path.abspath(img.filepath) if img.filepath else ""
            fp_l = fp.lower()
            if any(s in name_l or s in fp_l for s in subs_lower):
                old_name = img.name
                node.image = new_image
                replaced_nodes.add(node.name)
                matched_by_substring += 1
                print(f"  Material '{mat_name}' / Node '{node.name}' (image match): {old_name} -> {new_image.name}")

        if matched_by_substring == 0 and len(replaced_nodes) == 0:
            print(f"  WARNING: No Image Texture matched {image_substrings!r} in material '{mat_name}'")


print("Replacing image textures...")
for mat_name, spec in config["materials"].items():
    _replace_textures_in_material(mat_name, spec)

# ---------------------------------------------------------------------------
# 3. Configure render settings for speed
# ---------------------------------------------------------------------------
print("\nConfiguring render settings for fast CPU render...")

use_engine = args.engine.lower()

scene.render.resolution_x = args.resolution
scene.render.resolution_y = args.resolution
scene.render.resolution_percentage = 100

scene.render.fps = args.fps
scene.frame_start = 1
total_frames = int(args.duration * args.fps)
scene.frame_end = total_frames

if use_engine == "eevee":
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = args.samples
    scene.eevee.use_raytracing = False
    engine_label = "EEVEE"
else:
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.preview_samples = 1

    # OIDN denoiser cleans up the noise from ultra-low samples
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = "OPENIMAGEDENOISE"
    scene.cycles.denoising_input_passes = "RGB_ALBEDO_NORMAL"

    # Adaptive sampling — skip pixels that converge fast
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.5
    scene.cycles.adaptive_min_samples = 2

    # Fast GI replaces expensive global illumination
    scene.cycles.use_fast_gi = True
    scene.cycles.fast_gi_method = "REPLACE"

    # Absolute minimum light bounces
    scene.cycles.max_bounces = 2
    scene.cycles.diffuse_bounces = 1
    scene.cycles.glossy_bounces = 1
    scene.cycles.transmission_bounces = 0
    scene.cycles.volume_bounces = 0
    scene.cycles.transparent_max_bounces = 1

    # No caustics
    scene.cycles.caustics_reflective = False
    scene.cycles.caustics_refractive = False

    # Persistent data — caches BVH between frames (huge animation speedup)
    scene.render.use_persistent_data = True

    scene.render.use_motion_blur = False

    # Simplify — reduce geometry complexity
    scene.render.use_simplify = True
    scene.render.simplify_subdivision_render = 0

    # Let Blender auto-pick tile size for CPU
    scene.cycles.tile_size = 0
    scene.cycles.time_limit = 0

    # Strip all render passes down to minimum
    view_layer.use_pass_z = False
    view_layer.use_pass_mist = False
    view_layer.use_pass_normal = False
    view_layer.use_pass_vector = False
    view_layer.use_pass_emit = False
    view_layer.use_pass_environment = False
    view_layer.use_pass_shadow = False
    view_layer.use_pass_ambient_occlusion = False

    if scene.world:
        scene.world.light_settings.distance = 0

    engine_label = "Cycles CPU (max speed)"

# Output to FFMPEG / MP4
scene.render.image_settings.file_format = "FFMPEG"
scene.render.image_settings.color_mode = "RGB"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
scene.render.ffmpeg.ffmpeg_preset = "REALTIME"
scene.render.ffmpeg.gopsize = 12
scene.render.ffmpeg.audio_codec = "NONE"

scene.render.filepath = mp4_path
scene.render.use_overwrite = True
scene.render.use_file_extension = False

print(f"  Engine:     {engine_label}")
print(f"  Resolution: {args.resolution}x{args.resolution}")
print(f"  Frames:     {scene.frame_start}-{scene.frame_end} ({total_frames} frames @ {args.fps}fps = {args.duration}s)")
print(f"  Samples:    {args.samples}")
print(f"  Output:     {mp4_path}")

# ---------------------------------------------------------------------------
# 4. Render
# ---------------------------------------------------------------------------
print("\n=== Starting Render ===")
bpy.ops.render.render(animation=True)
print(f"\n=== Render Complete ===")
print(f"  MP4 saved: {mp4_path}")

# ---------------------------------------------------------------------------
# 5. Convert to GIF if requested
# ---------------------------------------------------------------------------
if wants_gif:
    gif_path = out_path
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        print(f"\nConverting to GIF: {gif_path}")
        palette_path = mp4_path + "_palette.png"
        subprocess.run([
            ffmpeg, "-y", "-i", mp4_path,
            "-vf", f"fps={args.fps},scale={min(args.resolution, 480)}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            gif_path,
        ], check=True)
        print(f"  GIF saved: {gif_path}")
        os.remove(mp4_path)
    else:
        print("\n  WARNING: ffmpeg not found in PATH, skipping GIF conversion.")
        print(f"  MP4 saved instead: {mp4_path}")
        print(f"  To convert manually: ffmpeg -i \"{mp4_path}\" \"{gif_path}\"")

print("\nDone!")
