bl_info = {
    "name": "Face Blur Workflow",
    "author": "OpenAI",
    "version": (0, 3, 1),
    "blender": (5, 2, 0),
    "location": "Movie Clip Editor > Sidebar > Face Blur",
    "description": "Reusable face-blur workflow with optional tracking and source-audio preservation for Blender 5.2",
    "category": "Movie Clip",
}

import bpy
import os


MASK_NAME = "FaceMasks"
TREE_NAME = "Face Blur Compositor"
AUDIO_STRIP_NAME = "Face Blur Source Audio"


def _active_clip(context):
    scene = context.scene
    if scene.active_clip:
        return scene.active_clip
    space = getattr(context, "space_data", None)
    if space and getattr(space, "type", None) == 'CLIP_EDITOR':
        return space.clip
    return None


def _ensure_mask():
    mask = bpy.data.masks.get(MASK_NAME)
    if mask is None:
        mask = bpy.data.masks.new(MASK_NAME)
    if len(mask.layers) == 0:
        mask.layers.new(name="Face 1")
    return mask


def _visible_socket(sockets, name):
    matches = [
        s for s in sockets
        if s.name == name and s.enabled and not s.is_unavailable
    ]
    if not matches:
        raise RuntimeError(f"Could not find active socket '{name}'")
    return matches[0]


def _clear_interface(tree):
    # Remove all existing group interface items safely.
    for item in list(tree.interface.items_tree):
        try:
            tree.interface.remove(item)
        except Exception:
            pass


def _set_blur_radius_on_node(node, radius):
    """Set Blur size using Blender 5.2's Size input socket.

    Blender 5.2 moved Blur settings such as Size and Type into node sockets.
    Older CompositorNodeBlur attributes such as size_x/size_y/filter_type no
    longer exist.
    """
    radius = float(radius)
    size_input = node.inputs.get("Size")
    if size_input is None:
        raise RuntimeError("Blur node has no Size input")

    value = size_input.default_value
    try:
        # NodeSocketVector in Blender 5.2; only X/Y are used.
        value[0] = radius
        value[1] = radius
    except Exception:
        try:
            size_input.default_value = (radius, radius, 0.0)
        except Exception as exc:
            raise RuntimeError(f"Could not set Blur Size input: {exc}") from exc


def _configure_mask_node_52(node, mask):
    """Configure Blender 5.2's compositor Mask node.

    In Blender 5.2, Size Source and Feather are node inputs rather than
    CompositorNodeMask RNA properties. Scene Size and Feather=True are the
    node defaults, so we preserve Scene Size and explicitly enable Feather
    through the input socket when available.
    """
    node.mask = mask

    feather_input = node.inputs.get("Feather")
    if feather_input is not None:
        try:
            feather_input.default_value = True
        except Exception:
            pass

    # "Size Source" is a Menu input in Blender 5.2 and defaults to Scene Size.
    # Do not use the removed legacy `size_source` property.
    size_source_input = node.inputs.get("Size Source")
    if size_source_input is None:
        raise RuntimeError("Mask node has no Size Source input in this Blender build")


def _build_compositor(scene, clip, mask):
    old = bpy.data.node_groups.get(TREE_NAME)
    if old is not None:
        # Do not delete a tree that is still being used elsewhere.
        if old.users == 0 or scene.compositing_node_group == old:
            try:
                bpy.data.node_groups.remove(old, do_unlink=True)
            except Exception:
                pass

    tree = bpy.data.node_groups.new(TREE_NAME, "CompositorNodeTree")
    scene.compositing_node_group = tree
    scene.render.use_compositing = True

    _clear_interface(tree)
    tree.interface.new_socket(
        name="Image",
        in_out='OUTPUT',
        socket_type='NodeSocketColor',
    )

    nodes = tree.nodes
    links = tree.links

    movie = nodes.new("CompositorNodeMovieClip")
    movie.name = "Face Blur Movie"
    movie.label = "Source Movie"
    movie.clip = clip
    movie.location = (-620, 80)

    blur = nodes.new("CompositorNodeBlur")
    blur.name = "Face Blur"
    blur.label = "Face Blur"
    # Blender 5.2's Blur node exposes Type/Size/Extend/Separable as sockets.
    # Type defaults to Gaussian and Separable defaults to enabled.
    _set_blur_radius_on_node(blur, scene.face_blur_radius)
    blur.location = (-390, -110)

    mask_node = nodes.new("CompositorNodeMask")
    mask_node.name = "Face Blur Mask"
    mask_node.label = MASK_NAME
    _configure_mask_node_52(mask_node, mask)
    mask_node.location = (-390, 230)

    mix = nodes.new("ShaderNodeMix")
    mix.name = "Face Blur Mix"
    mix.label = "Original + Blurred"
    mix.data_type = 'RGBA'
    mix.blend_type = 'MIX'
    mix.clamp_factor = True
    mix.location = (-80, 70)

    viewer = nodes.new("CompositorNodeViewer")
    viewer.name = "Face Blur Viewer"
    viewer.location = (220, -70)

    output = nodes.new("NodeGroupOutput")
    output.name = "Face Blur Output"
    output.is_active_output = True
    output.location = (220, 120)

    links.new(movie.outputs["Image"], blur.inputs["Image"])
    links.new(movie.outputs["Image"], _visible_socket(mix.inputs, "A"))
    links.new(blur.outputs["Image"], _visible_socket(mix.inputs, "B"))
    links.new(mask_node.outputs["Mask"], _visible_socket(mix.inputs, "Factor"))
    links.new(_visible_socket(mix.outputs, "Result"), viewer.inputs["Image"])
    links.new(_visible_socket(mix.outputs, "Result"), output.inputs["Image"])

    return tree


def _set_clip_editor(context, clip, mask):
    # Configure the editor the operator was launched from.
    space = getattr(context, "space_data", None)
    if space and getattr(space, "type", None) == 'CLIP_EDITOR':
        space.clip = clip
        space.mask = mask
        space.mode = 'MASK'

    # Also update any Movie Clip Editors visible in this window.
    screen = getattr(context, "screen", None)
    if screen:
        for area in screen.areas:
            if area.type == 'CLIP_EDITOR':
                area.spaces.active.clip = clip
                area.spaces.active.mask = mask
                area.spaces.active.mode = 'MASK'


def _set_clip_editors_after_file_browser(clip_name, mask_name):
    """Apply the clip after Blender closes the temporary file-browser UI."""
    def apply():
        clip = bpy.data.movieclips.get(clip_name)
        mask = bpy.data.masks.get(mask_name)
        if clip is None:
            return None

        wm = bpy.context.window_manager
        if wm is None:
            return None

        for window in wm.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type != 'CLIP_EDITOR':
                    continue
                space = area.spaces.active
                space.clip = clip
                if mask is not None:
                    space.mask = mask
                try:
                    space.mode = 'MASK'
                except Exception:
                    pass
                area.tag_redraw()

        scene = bpy.context.scene
        if scene is not None:
            scene.active_clip = clip
            scene.frame_set(scene.frame_current)

        return None

    bpy.app.timers.register(apply, first_interval=0.20)


def _remove_face_blur_audio(scene):
    """Remove only sound strips created by this add-on."""
    seq = scene.sequence_editor
    if seq is None:
        return

    for strip in list(seq.strips):
        owned = False
        try:
            owned = bool(strip.get("face_blur_source_audio", False))
        except Exception:
            pass
        if owned or strip.name == AUDIO_STRIP_NAME:
            seq.strips.remove(strip)


def _configure_audio_encoding(scene, enabled=True):
    """Configure Blender's video/audio encoding for MP4 output."""
    image_settings = scene.render.image_settings
    image_settings.media_type = 'VIDEO'

    ffmpeg = scene.render.ffmpeg
    ffmpeg.format = 'MPEG4'
    ffmpeg.codec = 'H264'

    if enabled:
        ffmpeg.audio_codec = 'AAC'
        try:
            ffmpeg.audio_bitrate = 192
        except Exception:
            pass
    else:
        ffmpeg.audio_codec = 'NONE'


def _sync_source_audio(scene, clip):
    """Add the source movie's audio stream as a VSE Sound strip.

    The compositor still supplies the image.  The Sequencer contains only
    audio, so Blender passes the composited image through while mixing the
    sound strip into the encoded movie.
    """
    _remove_face_blur_audio(scene)

    if not scene.face_blur_preserve_audio:
        _configure_audio_encoding(scene, enabled=False)
        scene["face_blur_audio_status"] = "Disabled"
        return None

    seq = scene.sequence_editor_create()

    # Avoid silently changing the picture if this scene already has unrelated
    # Sequencer content.  A visual strip would be processed after the
    # compositor when render.use_sequencer is enabled.
    foreign = []
    for strip in seq.strips:
        owned = False
        try:
            owned = bool(strip.get("face_blur_source_audio", False))
        except Exception:
            pass
        if not owned:
            foreign.append(strip)

    if foreign:
        names = ", ".join(strip.name for strip in foreign[:3])
        if len(foreign) > 3:
            names += ", ..."
        scene["face_blur_audio_status"] = "Skipped: existing Sequencer strips"
        raise RuntimeError(
            "Existing Sequencer strips were found "
            f"({names}). Audio preservation was not enabled because those "
            "strips could change the rendered picture."
        )

    src = bpy.path.abspath(clip.filepath)
    if not os.path.isfile(src):
        scene["face_blur_audio_status"] = "Source file not found"
        raise RuntimeError(f"Source movie was not found: {src}")

    try:
        sound_strip = seq.strips.new_sound(
            name=AUDIO_STRIP_NAME,
            filepath=src,
            channel=1,
            frame_start=int(scene.frame_start),
            stream=0,
        )
    except Exception as exc:
        scene["face_blur_audio_status"] = "No usable audio stream"
        raise RuntimeError(
            "Could not add the source movie's audio stream. "
            "The movie may not contain audio, or Blender could not decode it. "
            f"Details: {exc}"
        ) from exc

    sound_strip["face_blur_source_audio"] = True
    sound_strip.mute = False

    scene.use_audio = True
    scene.render.use_sequencer = True
    _configure_audio_encoding(scene, enabled=True)

    # Match the encoder sample rate to the source when Blender exposes it.
    try:
        sample_rate = int(sound_strip.sound.samplerate)
        if sample_rate > 0:
            scene.render.ffmpeg.audio_mixrate = sample_rate
    except Exception:
        pass

    scene["face_blur_audio_status"] = "Source audio ready (AAC)"
    return sound_strip


def _set_default_output(scene, clip):
    src = bpy.path.abspath(clip.filepath)
    base = os.path.splitext(os.path.basename(src))[0]
    folder = os.path.dirname(src)

    scene.render.filepath = os.path.join(folder, base + "_blurred.mp4")

    # Blender 5.2 separates media type from image file format.
    _configure_audio_encoding(
        scene,
        enabled=bool(scene.face_blur_preserve_audio),
    )




def _apply_blur_radius(scene):
    tree = scene.compositing_node_group
    if not tree:
        return
    node = tree.nodes.get("Face Blur")
    if node and node.bl_idname == "CompositorNodeBlur":
        _set_blur_radius_on_node(node, scene.face_blur_radius)


def _blur_radius_update(self, context):
    _apply_blur_radius(context.scene)


def _selected_or_active_mask_layer_index(mask):
    selected = [i for i, layer in enumerate(mask.layers) if layer.select]
    if len(selected) == 1:
        return selected[0]
    if not len(mask.layers):
        return None
    return max(0, min(mask.active_layer_index, len(mask.layers) - 1))


def _mask_for_navigation(context):
    space = getattr(context, "space_data", None)
    if space and getattr(space, "type", None) == 'CLIP_EDITOR':
        mask = getattr(space, "mask", None)
        if mask is not None:
            return mask
    return bpy.data.masks.get(MASK_NAME)


def _find_mask_layer_keyframe(context, direction):
    """Find prev/next shape key on only the selected/current mask layer."""
    mask = _mask_for_navigation(context)
    if mask is None or not len(mask.layers):
        raise RuntimeError("No mask is active")

    layer_index = _selected_or_active_mask_layer_index(mask)
    if layer_index is None:
        raise RuntimeError("No mask layer is active")

    area = context.area
    if area is None or area.type != 'CLIP_EDITOR':
        raise RuntimeError("Run this from the Movie Clip Editor Face Blur panel")

    region = _window_region(area)
    if region is None:
        raise RuntimeError("Movie Clip Editor window region was not found")

    scene = context.scene
    mask_start = int(mask.frame_start) if mask.frame_start else int(scene.frame_start)
    mask_end = int(mask.frame_end) if mask.frame_end else int(scene.frame_end)
    start = max(int(scene.frame_start), mask_start)
    end = min(int(scene.frame_end), mask_end)
    current = int(scene.frame_current)

    frames = (
        range(current + 1, end + 1)
        if direction > 0
        else range(current - 1, start - 1, -1)
    )

    temp_mask = mask.copy()
    temp_mask.name = "__FaceBlur_KeyScan__"
    for i, layer in enumerate(temp_mask.layers):
        layer.select = (i == layer_index)
    temp_mask.active_layer_index = layer_index

    space = area.spaces.active
    original_mask = getattr(space, "mask", None)
    original_mode = getattr(space, "mode", None)
    original_frame = current

    prefs_edit = context.preferences.edit
    original_global_undo = prefs_edit.use_global_undo
    found = None

    try:
        prefs_edit.use_global_undo = False
        space.mask = temp_mask
        try:
            space.mode = 'MASK'
        except Exception:
            pass

        for frame in frames:
            scene.frame_set(frame)
            with context.temp_override(
                area=area,
                region=region,
                space_data=space,
            ):
                result = bpy.ops.mask.shape_key_clear()

            if 'FINISHED' in result:
                found = frame
                break
    finally:
        space.mask = original_mask
        if original_mode is not None:
            try:
                space.mode = original_mode
            except Exception:
                pass
        prefs_edit.use_global_undo = original_global_undo

        try:
            bpy.data.masks.remove(temp_mask, do_unlink=True)
        except Exception:
            pass

        scene.frame_set(found if found is not None else original_frame)

    return found, mask.layers[layer_index].name


def _active_mask_layer():
    mask = _ensure_mask()
    if not len(mask.layers):
        return mask, None
    index = max(0, min(mask.active_layer_index, len(mask.layers) - 1))
    return mask, mask.layers[index]


def _active_spline(layer):
    if layer is None:
        return None
    if layer.splines.active is not None:
        return layer.splines.active
    if len(layer.splines):
        return layer.splines[-1]
    return None


def _select_only_active_layer_points(mask, layer):
    for candidate_layer in mask.layers:
        for spline in candidate_layer.splines:
            for point in spline.points:
                try:
                    point.select_control_point = False
                    point.select_left_handle = False
                    point.select_right_handle = False
                except Exception:
                    point.select = False

    for spline in layer.splines:
        for point in spline.points:
            try:
                point.select_control_point = True
                point.select_left_handle = True
                point.select_right_handle = True
            except Exception:
                point.select = True

    if len(layer.splines) and layer.splines.active is None:
        layer.splines.active = layer.splines[-1]


def _window_region(area):
    for region in area.regions:
        if region.type == 'WINDOW':
            return region
    return None


def _find_bottom_timeline_area(screen, exclude_area=None):
    """Find the wide editor strip at the bottom of the current workspace.

    The stock Motion Tracking workspace uses a wide lower editor.  Detect it
    by geometry instead of by editor type, because users may already have
    changed that editor between Clip/Graph/Dope Sheet/Timeline.
    """
    if screen is None or not screen.areas:
        return None

    screen_width = max((area.x + area.width) for area in screen.areas)
    min_width = max(500, int(screen_width * 0.45))

    candidates = [
        area for area in screen.areas
        if area != exclude_area
        and area.width >= min_width
        and area.height >= 60
        and area.type not in {'PROPERTIES', 'OUTLINER', 'FILE_BROWSER'}
    ]

    if not candidates:
        return None

    # Lowest Y wins; when tied, prefer the widest/shorter strip.
    candidates.sort(key=lambda area: (area.y, -area.width, area.height))
    return candidates[0]


def _find_area_by_pointer(screen, pointer):
    if screen is None or pointer is None:
        return None
    for area in screen.areas:
        try:
            if area.as_pointer() == pointer:
                return area
        except Exception:
            pass
    return None


def _configure_specific_area_as_mask_dopesheet(area):
    """Advance one step toward DOPESHEET_EDITOR / MASK.

    Returns:
      'DONE'       - already fully configured
      'CONVERTED'  - editor type changed; needs another UI tick
      'FAILED'     - could not configure
    """
    if area is None:
        return 'FAILED'

    if area.type != 'DOPESHEET_EDITOR':
        try:
            area.type = 'DOPESHEET_EDITOR'
            area.tag_redraw()
            return 'CONVERTED'
        except Exception:
            return 'FAILED'

    space = area.spaces.active
    try:
        space.mode = 'MASK'
    except Exception:
        try:
            space.ui_mode = 'MASK'
        except Exception:
            return 'FAILED'

    # Blender 5.2 exposes both mode and ui_mode.  Verify one accepted MASK.
    if (
        getattr(space, "mode", None) != 'MASK'
        and getattr(space, "ui_mode", None) != 'MASK'
    ):
        return 'FAILED'

    try:
        space.show_seconds = False
    except Exception:
        pass

    area.tag_redraw()
    return 'DONE'


def _configure_bottom_mask_dopesheet(screen, exclude_area=None):
    """Configure the wide bottom strip as Dope Sheet > Mask.

    Returns (status, target_pointer).
    """
    area = _find_bottom_timeline_area(screen, exclude_area=exclude_area)
    if area is None:
        return 'FAILED', None

    pointer = area.as_pointer()
    status = _configure_specific_area_as_mask_dopesheet(area)
    return status, pointer


def _bottom_mask_dopesheet_is_ready(screen, exclude_area=None):
    area = _find_bottom_timeline_area(screen, exclude_area=exclude_area)
    if area is None or area.type != 'DOPESHEET_EDITOR':
        return False
    space = area.spaces.active
    return (
        getattr(space, "mode", None) == 'MASK'
        or getattr(space, "ui_mode", None) == 'MASK'
    )


def _configure_bottom_mask_dopesheet_after_file_browser(
    main_area_pointer=None,
    target_area_pointer=None,
):
    """Finish/reapply the lower Mask Dope Sheet after the file browser closes."""
    state = {
        "remaining": 30,
        "target": target_area_pointer,
    }

    def apply():
        wm = bpy.context.window_manager
        if wm is None:
            return None

        finished_any = False

        for window in wm.windows:
            screen = window.screen
            if screen is None:
                continue

            exclude_area = _find_area_by_pointer(screen, main_area_pointer)

            area = _find_area_by_pointer(screen, state["target"])
            if area is None:
                area = _find_bottom_timeline_area(
                    screen,
                    exclude_area=exclude_area,
                )
                if area is not None:
                    try:
                        state["target"] = area.as_pointer()
                    except Exception:
                        pass

            status = _configure_specific_area_as_mask_dopesheet(area)
            if status == 'DONE':
                finished_any = True

        if finished_any:
            return None

        state["remaining"] -= 1
        if state["remaining"] <= 0:
            return None
        return 0.20

    bpy.app.timers.register(apply, first_interval=0.20)


class FACEBLUR_OT_configure_mask_dopesheet(bpy.types.Operator):
    bl_idname = "face_blur.configure_mask_dopesheet"
    bl_label = "Configure Bottom Mask Dope Sheet"
    bl_description = (
        "Configure the wide bottom editor as a Dope Sheet showing mask keyframes"
    )

    def execute(self, context):
        main_pointer = None
        try:
            if context.area is not None:
                main_pointer = context.area.as_pointer()
        except Exception:
            pass

        status, target_pointer = _configure_bottom_mask_dopesheet(
            context.screen,
            exclude_area=context.area,
        )

        if status == 'FAILED':
            self.report(
                {'ERROR'},
                "Could not find/configure the wide bottom editor in this workspace"
            )
            return {'CANCELLED'}

        if status == 'DONE':
            self.report({'INFO'}, "Bottom editor is Dope Sheet > Mask")
            return {'FINISHED'}

        # Editor type was changed.  Finish setting MASK mode on the next tick.
        _configure_bottom_mask_dopesheet_after_file_browser(
            main_area_pointer=main_pointer,
            target_area_pointer=target_pointer,
        )
        self.report({'INFO'}, "Converting bottom editor to Dope Sheet > Mask")
        return {'FINISHED'}


class FACEBLUR_OT_load_video(bpy.types.Operator):
    bl_idname = "face_blur.load_video"
    bl_label = "1. Load Video"
    bl_description = "Load a source video and prepare the reusable face-mask compositor"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(
        default="*.mov;*.mp4;*.m4v;*.avi;*.mkv;*.webm",
        options={'HIDDEN'},
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No video selected")
            return {'CANCELLED'}

        try:
            clip = bpy.data.movieclips.load(self.filepath, check_existing=True)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not load video: {exc}")
            return {'CANCELLED'}

        scene = context.scene
        scene.active_clip = clip
        clip.frame_start = 1

        # Make the full clip available on the timeline immediately.
        # Resolution/geometry is still matched later by "Match Geometry to Clip".
        scene.frame_start = 1
        scene.frame_end = max(1, int(clip.frame_duration))

        mask = _ensure_mask()
        mask.frame_start = scene.frame_start
        mask.frame_end = scene.frame_end

        # During a file-selection operator Blender temporarily replaces the
        # originating editor with the File Browser. Try immediately, then run
        # a short deferred update after the File Browser closes.
        _set_clip_editor(context, clip, mask)
        _set_clip_editors_after_file_browser(clip.name, mask.name)

        main_area_pointer = None
        try:
            if context.area is not None:
                main_area_pointer = context.area.as_pointer()
        except Exception:
            pass

        try:
            _build_compositor(scene, clip, mask)
        except Exception as exc:
            self.report({'ERROR'}, f"Video loaded, but compositor setup failed: {exc}")
            return {'CANCELLED'}

        # Configure the lower editor only after the core video/compositor
        # setup succeeds. Preserve the exact target area's pointer so the
        # delayed pass modifies the same lower strip.
        target_area_pointer = None
        try:
            _status, target_area_pointer = _configure_bottom_mask_dopesheet(
                context.screen,
                exclude_area=context.area,
            )
        except Exception:
            pass
        _configure_bottom_mask_dopesheet_after_file_browser(
            main_area_pointer=main_area_pointer,
            target_area_pointer=target_area_pointer,
        )

        # A previous source clip may have left an add-on-managed Sound strip.
        # Audio is recreated after Match Geometry, when the final FPS is known.
        _remove_face_blur_audio(scene)
        scene["face_blur_audio_status"] = (
            "Will sync after Match Geometry"
            if scene.face_blur_preserve_audio
            else "Disabled"
        )

        output_warning = None
        try:
            _set_default_output(scene, clip)
        except Exception as exc:
            output_warning = str(exc)

        scene.frame_set(1)

        if output_warning:
            self.report(
                {'WARNING'},
                "Video/compositor loaded, but automatic output setup failed: "
                + output_warning
            )
        else:
            self.report(
                {'INFO'},
                "Video loaded and full frame range set. Draw/keyframe masks, then match geometry before rendering."
            )
        return {'FINISHED'}


class FACEBLUR_OT_rebuild_compositor(bpy.types.Operator):
    bl_idname = "face_blur.rebuild_compositor"
    bl_label = "Rebuild Blur Nodes"
    bl_description = "Rebuild the standard face-blur compositor without changing masks"

    def execute(self, context):
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}
        mask = _ensure_mask()
        try:
            _build_compositor(context.scene, clip, mask)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not rebuild compositor: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Face-blur compositor rebuilt")
        return {'FINISHED'}


class FACEBLUR_OT_add_face_layer(bpy.types.Operator):
    bl_idname = "face_blur.add_face_layer"
    bl_label = "Add New Face Mask"
    bl_description = "Create a new independent mask layer for another face"

    def execute(self, context):
        mask = _ensure_mask()
        existing = {layer.name for layer in mask.layers}
        index = 1
        while f"Face {index}" in existing:
            index += 1
        layer = mask.layers.new(name=f"Face {index}")
        layer.alpha = 1.0
        layer.blend = 'ADD'
        layer.hide = False
        layer.hide_render = False
        mask.active_layer_index = len(mask.layers) - 1

        # Create a visible, editable default circle on the new layer.
        spline = layer.splines.new()
        target_points = 4
        current_points = len(spline.points)
        if current_points < target_points:
            spline.points.add(count=target_points - current_points)

        clip = _active_clip(context)
        if clip and clip.size[0] > 0 and clip.size[1] > 0:
            # Keep the default shape visually close to circular in pixel space.
            rx = 0.10
            ry = rx * (clip.size[0] / clip.size[1])
        else:
            rx = 0.10
            ry = 0.10

        coords = (
            (0.50, 0.50 + ry),
            (0.50 + rx, 0.50),
            (0.50, 0.50 - ry),
            (0.50 - rx, 0.50),
        )

        for point, co in zip(spline.points[:4], coords):
            point.co = co
            point.handle_type = 'AUTO'
            try:
                point.select_control_point = True
                point.select_left_handle = True
                point.select_right_handle = True
            except Exception:
                point.select = True

        spline.use_cyclic = True
        spline.use_fill = True
        layer.splines.active = spline

        if clip:
            _set_clip_editor(context, clip, mask)

        # Ensure the FaceMasks datablock is active and the new layer is selected.
        space = getattr(context, "space_data", None)
        if space and getattr(space, "type", None) == 'CLIP_EDITOR':
            space.mask = mask
            space.mode = 'MASK'

        self.report({'INFO'}, f"Created {layer.name} with a new circle")
        return {'FINISHED'}


class FACEBLUR_OT_previous_mask_key(bpy.types.Operator):
    bl_idname = "face_blur.previous_mask_key"
    bl_label = "Previous Mask Key"
    bl_description = "Jump to the previous shape key on only the current mask layer"

    def execute(self, context):
        try:
            frame, layer_name = _find_mask_layer_keyframe(context, -1)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not find previous mask key: {exc}")
            return {'CANCELLED'}

        if frame is None:
            self.report({'INFO'}, f"No earlier keyframe on {layer_name}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{layer_name}: previous keyframe is {frame}")
        return {'FINISHED'}


class FACEBLUR_OT_next_mask_key(bpy.types.Operator):
    bl_idname = "face_blur.next_mask_key"
    bl_label = "Next Mask Key"
    bl_description = "Jump to the next shape key on only the current mask layer"

    def execute(self, context):
        try:
            frame, layer_name = _find_mask_layer_keyframe(context, 1)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not find next mask key: {exc}")
            return {'CANCELLED'}

        if frame is None:
            self.report({'INFO'}, f"No later keyframe on {layer_name}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{layer_name}: next keyframe is {frame}")
        return {'FINISHED'}


class FACEBLUR_OT_create_track_for_active_mask(bpy.types.Operator):
    bl_idname = "face_blur.create_track_for_active_mask"
    bl_label = "Create Track for Active Face"
    bl_description = (
        "Create a motion-tracking marker at the center of the active face mask "
        "and switch to Tracking mode"
    )

    def execute(self, context):
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}

        mask, layer = _active_mask_layer()
        spline = _active_spline(layer)
        if layer is None or spline is None or not len(spline.points):
            self.report({'ERROR'}, "The active face layer has no mask spline")
            return {'CANCELLED'}

        count = len(spline.points)
        center_x = sum(point.co[0] for point in spline.points) / count
        center_y = sum(point.co[1] for point in spline.points) / count

        tracking_object = clip.tracking.objects.active
        if tracking_object is None:
            if len(clip.tracking.objects):
                tracking_object = clip.tracking.objects[0]
                clip.tracking.active_object_index = 0
            else:
                tracking_object = clip.tracking.objects.new("Camera")
                clip.tracking.active_object_index = len(clip.tracking.objects) - 1

        base_name = f"{layer.name} Track"
        existing_names = {track.name for track in tracking_object.tracks}
        track_name = base_name
        suffix = 2
        while track_name in existing_names:
            track_name = f"{base_name} {suffix}"
            suffix += 1

        frame = max(1, int(context.scene.frame_current))
        track = tracking_object.tracks.new(name=track_name, frame=frame)
        marker = track.markers.find_frame(frame)
        if marker is None:
            marker = track.markers.insert_frame(frame, co=(center_x, center_y))
        else:
            marker.co = (center_x, center_y)

        for candidate in tracking_object.tracks:
            candidate.select = False
        track.select = True
        tracking_object.tracks.active = track

        space = getattr(context, "space_data", None)
        if space and getattr(space, "type", None) == 'CLIP_EDITOR':
            space.clip = clip
            space.mode = 'TRACKING'

        self.report(
            {'INFO'},
            f"Created {track.name}. Track it, then click Parent Active Face to Track."
        )
        return {'FINISHED'}


class FACEBLUR_OT_parent_active_mask_to_track(bpy.types.Operator):
    bl_idname = "face_blur.parent_active_mask_to_track"
    bl_label = "Parent Active Face to Track"
    bl_description = (
        "Parent all points in the active face-mask layer to the active motion track "
        "while preserving the mask position"
    )

    def execute(self, context):
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}

        tracking_object = clip.tracking.objects.active
        active_track = tracking_object.tracks.active if tracking_object else None
        if active_track is None:
            self.report({'ERROR'}, "No active motion track. Create or select a track first.")
            return {'CANCELLED'}

        mask, layer = _active_mask_layer()
        if layer is None or not len(layer.splines):
            self.report({'ERROR'}, "The active face layer has no mask")
            return {'CANCELLED'}

        _select_only_active_layer_points(mask, layer)

        area = context.area
        if area is None or area.type != 'CLIP_EDITOR':
            self.report({'ERROR'}, "Run this from the Movie Clip Editor")
            return {'CANCELLED'}

        space = area.spaces.active
        space.clip = clip
        space.mask = mask
        space.mode = 'MASK'

        region = _window_region(area)
        try:
            if region is not None:
                with context.temp_override(area=area, region=region, space_data=space):
                    result = bpy.ops.mask.parent_set()
            else:
                result = bpy.ops.mask.parent_set()
        except Exception as exc:
            self.report({'ERROR'}, f"Could not parent mask to track: {exc}")
            return {'CANCELLED'}

        if 'FINISHED' not in result:
            self.report({'ERROR'}, "Blender did not parent the mask")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{layer.name} is now parented to {active_track.name}")
        return {'FINISHED'}


class FACEBLUR_OT_clear_active_mask_parent(bpy.types.Operator):
    bl_idname = "face_blur.clear_active_mask_parent"
    bl_label = "Clear Tracking Parent"
    bl_description = "Remove tracking parenting from the active face-mask layer"

    def execute(self, context):
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}

        mask, layer = _active_mask_layer()
        if layer is None or not len(layer.splines):
            self.report({'ERROR'}, "The active face layer has no mask")
            return {'CANCELLED'}

        _select_only_active_layer_points(mask, layer)

        area = context.area
        if area is None or area.type != 'CLIP_EDITOR':
            self.report({'ERROR'}, "Run this from the Movie Clip Editor")
            return {'CANCELLED'}

        space = area.spaces.active
        space.clip = clip
        space.mask = mask
        space.mode = 'MASK'

        region = _window_region(area)
        try:
            if region is not None:
                with context.temp_override(area=area, region=region, space_data=space):
                    result = bpy.ops.mask.parent_clear()
            else:
                result = bpy.ops.mask.parent_clear()
        except Exception as exc:
            self.report({'ERROR'}, f"Could not clear mask parenting: {exc}")
            return {'CANCELLED'}

        if 'FINISHED' not in result:
            self.report({'ERROR'}, "Blender did not clear the mask parent")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Cleared tracking parent from {layer.name}")
        return {'FINISHED'}


class FACEBLUR_OT_match_geometry(bpy.types.Operator):
    bl_idname = "face_blur.match_geometry"
    bl_label = "3. Match Geometry to Clip"
    bl_description = "Match output resolution, frame rate, and frame range to the loaded video"

    def execute(self, context):
        scene = context.scene
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}

        width, height = int(clip.size[0]), int(clip.size[1])
        if width <= 0 or height <= 0:
            self.report({'ERROR'}, "Blender has not detected the clip dimensions yet")
            return {'CANCELLED'}

        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0

        if clip.fps > 0:
            # Blender represents rates such as 29.97 as 30 / 1.001...
            nominal = max(1, int(round(clip.fps)))
            scene.render.fps = nominal
            scene.render.fps_base = nominal / float(clip.fps)

        clip.frame_start = 1
        scene.frame_start = 1
        scene.frame_end = max(1, int(clip.frame_duration))

        mask = _ensure_mask()
        mask.frame_start = scene.frame_start
        mask.frame_end = scene.frame_end

        audio_note = ""
        if scene.face_blur_preserve_audio:
            try:
                _sync_source_audio(scene, clip)
                audio_note = ", source audio ready"
            except Exception as exc:
                self.report(
                    {'WARNING'},
                    "Geometry matched, but source audio could not be configured: "
                    + str(exc)
                )
        else:
            _remove_face_blur_audio(scene)
            _configure_audio_encoding(scene, enabled=False)
            scene["face_blur_audio_status"] = "Disabled"

        self.report(
            {'INFO'},
            f"Matched {width}x{height}, {clip.fps:.3f} fps, "
            f"{clip.frame_duration} frames{audio_note}"
        )
        return {'FINISHED'}


class FACEBLUR_OT_render_animation(bpy.types.Operator):
    bl_idname = "face_blur.render_animation"
    bl_label = "4. Render Animation"
    bl_description = "Render the configured full animation"

    def execute(self, context):
        clip = _active_clip(context)
        if not clip:
            self.report({'ERROR'}, "Load a video first")
            return {'CANCELLED'}

        scene = context.scene
        w, h = clip.size
        if int(scene.render.resolution_x) != int(w) or int(scene.render.resolution_y) != int(h):
            self.report(
                {'ERROR'},
                f"Geometry mismatch: scene is {scene.render.resolution_x}x{scene.render.resolution_y}, "
                f"clip is {w}x{h}. Click Match Geometry to Clip first."
            )
            return {'CANCELLED'}

        try:
            if scene.face_blur_preserve_audio:
                _sync_source_audio(scene, clip)
            else:
                _remove_face_blur_audio(scene)
                _configure_audio_encoding(scene, enabled=False)
                scene["face_blur_audio_status"] = "Disabled"
        except Exception as exc:
            self.report({'ERROR'}, "Audio setup failed: " + str(exc))
            return {'CANCELLED'}

        bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
        return {'FINISHED'}


class FACEBLUR_PT_panel(bpy.types.Panel):
    bl_label = "Face Blur Workflow"
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Face Blur"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        clip = _active_clip(context)

        layout.operator("face_blur.load_video", icon='FILE_MOVIE')

        box = layout.box()
        box.label(text="2. Draw / keyframe masks")
        box.operator(
            "face_blur.configure_mask_dopesheet",
            text="Configure Bottom Mask Dope Sheet",
        )
        ready = _bottom_mask_dopesheet_is_ready(
            context.screen,
            exclude_area=context.area,
        )
        box.label(
            text="Bottom editor: Mask Dope Sheet" if ready
                 else "Bottom editor: NOT Mask Dope Sheet",
            icon='CHECKMARK' if ready else 'ERROR',
        )
        box.operator("face_blur.add_face_layer", text="Add New Face Mask", icon='ADD')
        mask = bpy.data.masks.get(MASK_NAME)
        if mask and len(mask.layers):
            layer_index = _selected_or_active_mask_layer_index(mask)
            active = mask.layers[layer_index]
            box.label(text=f"Current layer: {active.name} ({len(mask.layers)} total)")

            nav = box.row(align=True)
            nav.operator(
                "face_blur.previous_mask_key",
                text="Previous Key",
                icon='PREV_KEYFRAME',
            )
            nav.operator(
                "face_blur.next_mask_key",
                text="Next Key",
                icon='NEXT_KEYFRAME',
            )

        box.prop(scene, "face_blur_radius")

        tracking_box = box.box()
        tracking_box.prop(
            scene,
            "face_blur_enable_tracking",
            text="Enable Optional Motion Tracking",
        )
        if scene.face_blur_enable_tracking:
            tracking_box.label(text="Manual keyframing remains the default.", icon='INFO')
            tracking_box.operator(
                "face_blur.create_track_for_active_mask",
                text="Create Track for Active Face",
            )

            active_track = None
            if clip and clip.tracking.objects.active:
                active_track = clip.tracking.objects.active.tracks.active
            if active_track:
                tracking_box.label(text=f"Active track: {active_track.name}")

            tracking_box.label(text="Track the marker, then:")
            tracking_box.operator(
                "face_blur.parent_active_mask_to_track",
                text="Parent Active Face to Track",
            )
            tracking_box.operator(
                "face_blur.clear_active_mask_parent",
                text="Clear Tracking Parent",
            )

        if clip:
            box.label(text=f"Clip: {clip.name}")
            w, h = clip.size
            if w and h:
                box.label(text=f"Source: {w} x {h} @ {clip.fps:.3f} fps")

        audio_box = layout.box()
        audio_box.prop(
            scene,
            "face_blur_preserve_audio",
            text="Preserve Source Audio",
        )
        audio_status = scene.get(
            "face_blur_audio_status",
            "Will sync after Match Geometry"
            if scene.face_blur_preserve_audio
            else "Disabled",
        )
        audio_box.label(
            text=f"Audio: {audio_status}",
            icon='SPEAKER' if scene.face_blur_preserve_audio else 'MUTE_IPO_OFF',
        )

        layout.operator("face_blur.match_geometry", icon='FULLSCREEN_ENTER')

        if clip and clip.size[0] and clip.size[1]:
            w, h = clip.size
            ok = (
                scene.render.resolution_x == w
                and scene.render.resolution_y == h
                and scene.render.resolution_percentage == 100
            )
            row = layout.row()
            row.label(
                text=(
                    f"Geometry: {scene.render.resolution_x} x "
                    f"{scene.render.resolution_y} @ {scene.render.resolution_percentage}%"
                ),
                icon='CHECKMARK' if ok else 'ERROR',
            )

        layout.separator()
        layout.operator("face_blur.render_animation", icon='RENDER_ANIMATION')

        layout.separator()
        layout.operator("face_blur.rebuild_compositor", icon='NODETREE')
        layout.label(text="Audio is encoded as AAC when Preserve Source Audio is enabled.", icon='INFO')


classes = (
    FACEBLUR_OT_configure_mask_dopesheet,
    FACEBLUR_OT_load_video,
    FACEBLUR_OT_rebuild_compositor,
    FACEBLUR_OT_add_face_layer,
    FACEBLUR_OT_previous_mask_key,
    FACEBLUR_OT_next_mask_key,
    FACEBLUR_OT_create_track_for_active_mask,
    FACEBLUR_OT_parent_active_mask_to_track,
    FACEBLUR_OT_clear_active_mask_parent,
    FACEBLUR_OT_match_geometry,
    FACEBLUR_OT_render_animation,
    FACEBLUR_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.face_blur_radius = bpy.props.IntProperty(
        name="Blur Radius",
        description="Gaussian blur radius in pixels",
        default=100,
        min=1,
        max=2048,
        update=_blur_radius_update,
    )

    bpy.types.Scene.face_blur_enable_tracking = bpy.props.BoolProperty(
        name="Optional Motion Tracking",
        description="Show optional tracking helpers for the active face mask",
        default=False,
    )

    bpy.types.Scene.face_blur_preserve_audio = bpy.props.BoolProperty(
        name="Preserve Source Audio",
        description=(
            "Include the source movie's audio in the rendered MP4 "
            "using a Sequencer Sound strip and AAC encoding"
        ),
        default=True,
    )


def unregister():
    if hasattr(bpy.types.Scene, "face_blur_preserve_audio"):
        del bpy.types.Scene.face_blur_preserve_audio
    if hasattr(bpy.types.Scene, "face_blur_enable_tracking"):
        del bpy.types.Scene.face_blur_enable_tracking
    if hasattr(bpy.types.Scene, "face_blur_radius"):
        del bpy.types.Scene.face_blur_radius

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
