import bpy


def api():
    from . import presets
    return presets


def animated(preset):
    p = api()
    found = p.find_preset_control(preset)

    return bool(
        preset.auto_keyframe_enabled
        or p._matching_curves(
            preset.id_data,
            p._scene_preset_paths(preset.id_data, preset),
        )
        or (
            found
            and p._matching_curves(
                found[0],
                p.control_data_paths(*found),
            )
        )
    )


def members(preset):
    return {
        (key.as_pointer(), kb.name): (key, kb)
        for key, kb in api()._iter_preset_key_blocks(preset)
    }


def owners(pairs, exclude=None):
    result = []

    for scene in bpy.data.scenes:
        for preset in getattr(scene, "skv_global_presets", ()):
            if exclude is not None and preset == exclude:
                continue

            if set(pairs) & set(members(preset)) and animated(preset):
                result.append(preset)

    return result


def direct(pairs):
    p = api()
    result = []

    for key, kb in pairs.values():
        paths = {p._shape_key_value_data_path(kb.name)}
        entry = p._autokf_get_entry(key, kb.name, create=False)

        if p._matching_curves(key, paths) or (entry and entry.enabled):
            result.append((key, kb))

    return result


def conflict_message(preset):
    if not animated(preset):
        return ""

    pairs = members(preset)

    if direct(pairs):
        return (
            "Conflicting shape key animation: "
            "preset application is paused."
        )

    if owners(pairs, exclude=preset):
        return (
            "Overlapping animated presets: "
            "preset application is paused."
        )

    return ""


def plan(operator, context):
    p = api()
    mode = operator.policy_mode
    scene = context.scene

    preset = None
    detach = []
    erase = []
    messages = []
    extra_scenes = []

    if mode == "skv.auto_keyframe_toggle":
        obj = p.get_active_object(context)
        key = p.get_shape_key_data(obj) if obj else None
        kb = key.key_blocks.get(operator.key_name) if key else None

        if kb is None or kb == key.key_blocks[0]:
            raise RuntimeError("Choose a non-basis shape key.")

        pairs = {
            (key.as_pointer(), kb.name): (key, kb),
        }

        entry = p._autokf_get_entry(key, kb.name, create=False)
        enabling = not bool(entry and entry.enabled)

        if enabling:
            detach = owners(pairs)

            for other in detach:
                p._check_writable(other.id_data)
                extra_scenes.append(other.id_data)
                messages.append(
                    f"Remove {kb.name} from "
                    f"{other.id_data.name} / {other.name}"
                )
        else:
            erase = list(pairs.values())
            messages.append(
                f"Delete value animation: {key.name} / {kb.name}"
            )

    else:
        if not 0 <= operator.preset_index < len(scene.skv_global_presets):
            raise RuntimeError("Choose a preset.")

        preset = scene.skv_global_presets[operator.preset_index]

        if mode == "skv.add_selected_to_preset":
            obj = p.get_active_object(context)
            key = p.get_shape_key_data(obj) if obj else None

            if key is None:
                raise RuntimeError("Choose an object with shape keys.")

            existing = {
                item.key_name
                for item in preset.items
                if p.preset_item_object(item) == obj
            }

            pairs = {
                (key.as_pointer(), name): (key, key.key_blocks[name])
                for name in p.kd_visible_selected_names(
                    key,
                    scene.skv_props,
                )
                if name in key.key_blocks
                and name not in existing
                and not p._is_basis_name(key, name)
            }

            if not pairs:
                raise RuntimeError("No new shape keys selected.")

            enabling = animated(preset)

        else:
            pairs = members(preset)
            enabling = not preset.auto_keyframe_enabled
            obj = None

            if not enabling:
                messages.append(
                    f"Delete preset value animation: {preset.name}"
                )

        if enabling:
            if not pairs:
                raise RuntimeError(
                    "Add shape keys to the preset first."
                )

            overlaps = owners(pairs, exclude=preset)

            if overlaps:
                raise RuntimeError(
                    "Already controlled by animated presets: "
                    + ", ".join(item.name for item in overlaps)
                )

            erase = direct(pairs)

            messages.extend(
                f"Delete value animation: {key.name} / {kb.name}"
                for key, kb in erase
            )

    keys = {
        key.as_pointer(): key
        for key, kb in pairs.values()
    }

    if preset is not None:
        p._check_writable(scene)
        found = p.find_preset_control(preset)

        if found:
            key, ctrl = found
            keys[key.as_pointer()] = key

            if mode != "skv.add_selected_to_preset":
                if len(list(p._control_users(key, ctrl.name))) > 1:
                    raise RuntimeError(
                        "This legacy control is shared by several presets."
                    )

                p._reject_nla_and_drivers(
                    key,
                    p.control_data_paths(key, ctrl),
                )

        elif preset.control_uid:
            raise RuntimeError(
                "Restore the missing preset control first."
            )

        if mode != "skv.add_selected_to_preset":
            paths = p._scene_preset_paths(scene, preset)
            p._reject_nla_and_drivers(scene, paths)

            if enabling and p._matching_curves(scene, paths):
                raise RuntimeError(
                    "Update legacy preset animation first."
                )

    for key in keys.values():
        p._check_writable(key)

    for key, kb in erase:
        p._reject_nla_and_drivers(
            key,
            {p._shape_key_value_data_path(kb.name)},
        )

    if mode == "skv.auto_keyframe_toggle":
        p._reject_nla_and_drivers(
            key,
            {p._shape_key_value_data_path(kb.name)},
        )

    token = repr((
        mode,
        enabling,
        sorted(pairs),
        preset.as_pointer() if preset else 0,
        [
            (item.id_data.as_pointer(), item.as_pointer())
            for item in detach
        ],
        [
            (key.as_pointer(), kb.name)
            for key, kb in erase
        ],
        messages,
    ))

    return {
        "mode": mode,
        "preset": preset,
        "pairs": pairs,
        "keys": list(keys.values()),
        "detach": detach,
        "erase": erase,
        "enabling": enabling,
        "messages": messages,
        "scenes": extra_scenes,
        "token": token,
        "obj": obj if mode == "skv.add_selected_to_preset" else None,
    }


def invoke(operator, context, event):
    operator.confirmation_token = ""

    try:
        work = plan(operator, context)
    except Exception as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    if work["messages"]:
        operator.confirmation_token = work["token"]
        return context.window_manager.invoke_props_dialog(
            operator,
            width=580,
        )

    return execute(operator, context)


def draw(operator, context):
    try:
        work = plan(operator, context)

        operator.layout.label(
            text="Confirm the following changes:",
            icon="ERROR",
        )

        for message in work["messages"]:
            operator.layout.label(text=message)

        operator.layout.label(
            text="Unrelated animation channels will be preserved."
        )

    except Exception as exc:
        operator.layout.label(text=str(exc), icon="ERROR")


def insert(key, path, frame):
    p = api()
    p._reject_nla_and_drivers(key, {path})

    anim = key.animation_data

    if anim and anim.action:
        for holder in p._action_bindings(key):
            if holder != anim and holder.action == anim.action:
                raise RuntimeError(
                    "The active Action is also used in NLA."
                )

    p._isolate_actions(key, active_only=True)
    before = key.animation_data.action if key.animation_data else None

    try:
        if not key.keyframe_insert(data_path=path, frame=frame):
            raise RuntimeError("Cannot insert the keyframe.")
    finally:
        after = (
            key.animation_data.action
            if key.animation_data
            else None
        )

        if p._CONTROL_TRANSACTION and after and after != before:
            p._CONTROL_TRANSACTION.track_created_action(after)


def execute(operator, context):
    p = api()

    try:
        work = plan(operator, context)

        if (
            work["messages"]
            and operator.confirmation_token != work["token"]
        ):
            raise RuntimeError(
                "Confirmation is required. Run the button again."
            )

    except Exception as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    class Edit(p.ControlTransaction):
        def __enter__(self):
            self.extra = {
                scene.as_pointer(): (
                    scene,
                    p._snapshot_presets(scene),
                    scene.skv_global_preset_index,
                )
                for scene in work["scenes"]
                if scene != context.scene
            }

            self.autokeys = [
                (
                    key,
                    [
                        (
                            entry.name,
                            entry.enabled,
                            entry.last_frame,
                            entry.last_value,
                        )
                        for entry in key.skv_auto_keyframes
                    ],
                )
                for key in self.keys
            ]

            return super().__enter__()

        def rollback(self):
            errors = []

            try:
                super().rollback()
            except Exception as exc:
                errors.append(str(exc))

            for scene, snapshot, index in self.extra.values():
                try:
                    p._restore_presets(scene, snapshot, index)
                except Exception as exc:
                    errors.append(str(exc))

            for key, entries in self.autokeys:
                try:
                    key.skv_auto_keyframes.clear()

                    for name, enabled, frame, value in entries:
                        entry = key.skv_auto_keyframes.add()
                        entry.name = name
                        entry.enabled = enabled
                        entry.last_frame = frame
                        entry.last_value = value

                except Exception as exc:
                    errors.append(str(exc))

            if errors:
                raise RuntimeError(
                    "Rollback incomplete: " + "; ".join(errors)
                )

    try:
        with Edit(context.scene, work["keys"]):
            for key, kb in work["erase"]:
                p._delete_paths(
                    key,
                    {p._shape_key_value_data_path(kb.name)},
                )

                entry = p._autokf_get_entry(
                    key,
                    kb.name,
                    create=False,
                )

                if entry:
                    entry.enabled = False

            for other in work["detach"]:
                remove = []

                for index, item in enumerate(other.items):
                    obj = p.preset_item_object(item)
                    key = p.get_shape_key_data(obj) if obj else None

                    if key and (
                        key.as_pointer(),
                        item.key_name,
                    ) in work["pairs"]:
                        remove.append(index)

                for index in reversed(remove):
                    other.items.remove(index)

                if not other.items:
                    other.auto_keyframe_enabled = False

            preset = work["preset"]
            frame = context.scene.frame_current

            if work["mode"] == "skv.auto_keyframe_toggle":
                key, kb = next(iter(work["pairs"].values()))

                entry = p._autokf_get_entry(
                    key,
                    kb.name,
                    create=True,
                )
                entry.enabled = work["enabling"]
                entry.last_frame = frame
                entry.last_value = float(kb.value)

                if entry.enabled:
                    insert(
                        key,
                        p._shape_key_value_data_path(kb.name),
                        frame,
                    )

            elif work["mode"] == "skv.add_selected_to_preset":
                for key, kb in work["pairs"].values():
                    if not p.add_shape_key_to_preset(
                        preset,
                        work["obj"],
                        kb.name,
                    ):
                        raise RuntimeError(
                            "Cannot add shape key: " + kb.name
                        )

            elif work["enabling"]:
                found = p.ensure_preset_control(preset)

                if found is None:
                    raise RuntimeError(
                        "Cannot create the preset control."
                    )

                key, ctrl = found
                insert(key, p.control_data_path(ctrl), frame)
                preset.auto_keyframe_enabled = True

            else:
                found = p.find_preset_control(preset)

                if found:
                    p._delete_paths(
                        found[0],
                        p.control_data_paths(*found),
                    )

                p._delete_paths(
                    context.scene,
                    p._scene_preset_paths(context.scene, preset),
                )
                preset.auto_keyframe_enabled = False

    except Exception as exc:
        operator.report({"ERROR"}, str(exc))
        return {"FINISHED"}

    p.tag_redraw_view3d(context)
    return {"FINISHED"}


def control_changed(ctrl, context):
    p = api()

    if p._CONTROL_FRAME_GUARD or p._CONTROL_TRANSACTION is not None:
        return

    users = [
        preset
        for scene, preset in p._control_users(ctrl.id_data, ctrl.name)
        if scene == context.scene and preset.auto_keyframe_enabled
    ]

    if not users or any(conflict_message(preset) for preset in users):
        return

    try:
        p._reject_nla_and_drivers(
            ctrl.id_data,
            p.control_data_paths(ctrl.id_data, ctrl),
        )
        insert(
            ctrl.id_data,
            p.control_data_path(ctrl),
            context.scene.frame_current,
        )

    except Exception as exc:
        print("MorphDeck: preset auto keyframe failed:", exc)