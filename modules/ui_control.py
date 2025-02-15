import os
import gradio as gr
from modules.control import unit
from modules.control import processors # patrickvonplaten controlnet_aux
from modules.control.units import controlnet # lllyasviel ControlNet
from modules.control.units import xs # vislearn ControlNet-XS
from modules.control.units import lite # vislearn ControlNet-XS
from modules.control.units import t2iadapter # TencentARC T2I-Adapter
from modules.control.units import reference # reference pipeline
from modules.control.units import ipadapter # reference pipeline
from modules import errors, shared, progress, sd_samplers, ui, ui_components, ui_symbols, ui_common, generation_parameters_copypaste, call_queue
from modules.ui_components import FormRow, FormGroup


gr_height = 512
max_units = 5
units: list[unit.Unit] = [] # main state variable
input_source = None
input_init = None
debug = shared.log.trace if os.environ.get('SD_CONTROL_DEBUG', None) is not None else lambda *args, **kwargs: None
debug('Trace: CONTROL')


def initialize():
    from modules import devices
    shared.log.debug(f'Control initialize: models={shared.opts.control_dir}')
    controlnet.cache_dir = os.path.join(shared.opts.control_dir, 'controlnet')
    xs.cache_dir = os.path.join(shared.opts.control_dir, 'xs')
    lite.cache_dir = os.path.join(shared.opts.control_dir, 'lite')
    t2iadapter.cache_dir = os.path.join(shared.opts.control_dir, 'adapter')
    processors.cache_dir = os.path.join(shared.opts.control_dir, 'processor')
    unit.default_device = devices.device
    unit.default_dtype = devices.dtype
    os.makedirs(shared.opts.control_dir, exist_ok=True)
    os.makedirs(controlnet.cache_dir, exist_ok=True)
    os.makedirs(xs.cache_dir, exist_ok=True)
    os.makedirs(lite.cache_dir, exist_ok=True)
    os.makedirs(t2iadapter.cache_dir, exist_ok=True)
    os.makedirs(processors.cache_dir, exist_ok=True)


def return_controls(res):
    # return preview, image, video, gallery, text
    debug(f'Control received: type={type(res)} {res}')
    if isinstance(res, str): # error response
        return [None, None, None, None, res]
    elif isinstance(res, tuple): # standard response received as tuple via control_run->yield(output_images, process_image, result_txt)
        preview_image = res[1] # may be None
        output_image = res[0][0] if isinstance(res[0], list) else res[0] # may be image or list of images
        if isinstance(res[0], list):
            output_gallery = res[0] if res[0][0] is not None else []
        else:
            output_gallery = [res[0]] if res[0] is not None else [] # must return list, but can receive single image
        result_txt = res[2] if len(res) > 2 else '' # do we have a message
        output_video = res[3] if len(res) > 3 else None # do we have a video filename
        return [preview_image, output_image, output_video, output_gallery, result_txt]
    else: # unexpected
        return [None, None, None, None, f'Control: Unexpected response: {type(res)}']


def generate_click(job_id: str, active_tab: str, *args):
    from modules.control.run import control_run
    shared.log.debug(f'Control: tab={active_tab} job={job_id} args={args}')
    if active_tab not in ['controlnet', 'xs', 'adapter', 'reference', 'lite']:
        return None, None, None, None, f'Control: Unknown mode: {active_tab} args={args}'
    shared.state.begin('control')
    progress.add_task_to_queue(job_id)
    with call_queue.queue_lock:
        yield [None, None, None, None, 'Control: starting']
        shared.mem_mon.reset()
        progress.start_task(job_id)
        try:
            for results in control_run(units, input_source, input_init, active_tab, True, *args):
                progress.record_results(job_id, results)
                yield return_controls(results)
        except Exception as e:
            shared.log.error(f"Control exception: {e}")
            errors.display(e, 'Control')
            return None, None, None, None, f'Control: Exception: {e}'
        progress.finish_task(job_id)
    shared.state.end()


def display_units(num_units):
    return (num_units * [gr.update(visible=True)]) + ((max_units - num_units) * [gr.update(visible=False)])


def get_video(filepath: str):
    try:
        import cv2
        from modules.control.util import decode_fourcc
        video = cv2.VideoCapture(filepath)
        if not video.isOpened():
            msg = f'Control: video open failed: path="{filepath}"'
            shared.log.error(msg)
            return msg
        frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = video.get(cv2.CAP_PROP_FPS)
        duration = float(frames) / fps
        w, h = int(video.get(cv2.CAP_PROP_FRAME_WIDTH)), int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
        codec = decode_fourcc(video.get(cv2.CAP_PROP_FOURCC))
        video.release()
        shared.log.debug(f'Control: input video: path={filepath} frames={frames} fps={fps} size={w}x{h} codec={codec}')
        msg = f'Control input | Video | Size {w}x{h} | Frames {frames} | FPS {fps:.2f} | Duration {duration:.2f} | Codec {codec}'
        return msg
    except Exception as e:
        msg = f'Control: video open failed: path={filepath} {e}'
        shared.log.error(msg)
        return msg


def select_input(selected_input, selected_init, init_type):
    debug(f'Control select input: source={selected_input} init={selected_init}, type={init_type}')
    global input_source, input_init # pylint: disable=global-statement
    input_type = type(selected_input)
    status = 'Control input | Unknown'
    res = [gr.Tabs.update(selected='out-gallery'), status]
    # control inputs
    if hasattr(selected_input, 'size'): # image via upload -> image
        input_source = [selected_input]
        input_type = 'PIL.Image'
        shared.log.debug(f'Control input: type={input_type} input={input_source}')
        status = f'Control input | Image | Size {selected_input.width}x{selected_input.height} | Mode {selected_input.mode}'
        res = [gr.Tabs.update(selected='out-gallery'), status]
    elif isinstance(selected_input, gr.components.image.Image): # not likely
        input_source = [selected_input.value]
        input_type = 'gr.Image'
        shared.log.debug(f'Control input: type={input_type} input={input_source}')
        res = [gr.Tabs.update(selected='out-gallery'), status]
    elif isinstance(selected_input, str): # video via upload > tmp filepath to video
        input_source = selected_input
        input_type = 'gr.Video'
        shared.log.debug(f'Control input: type={input_type} input={input_source}')
        status = get_video(input_source)
        res = [gr.Tabs.update(selected='out-video'), status]
    elif isinstance(selected_input, list): # batch or folder via upload -> list of tmp filepaths
        if hasattr(selected_input[0], 'name'):
            input_type = 'tempfiles'
            input_source = [f.name for f in selected_input] # tempfile
        else:
            input_type = 'files'
            input_source = selected_input
        status = f'Control input | Images | Files {len(input_source)}'
        shared.log.debug(f'Control input: type={input_type} input={input_source}')
        res = [gr.Tabs.update(selected='out-gallery'), status]
    else: # unknown
        input_source = None
    # init inputs: optional
    if init_type == 0: # Control only
        input_init = None
    elif init_type == 1: # Init image same as control assigned during runtime
        input_init = None
    elif init_type == 2: # Separate init image
        if hasattr(selected_init, 'size'): # image via upload -> image
            input_init = [selected_init]
            input_type = 'PIL.Image'
            shared.log.debug(f'Control input: type={input_type} input={input_init}')
            status = f'Control input | Image | Size {selected_init.width}x{selected_init.height} | Mode {selected_init.mode}'
            res = [gr.Tabs.update(selected='out-gallery'), status]
        elif isinstance(selected_init, gr.components.image.Image): # not likely
            input_init = [selected_init.value]
            input_type = 'gr.Image'
            shared.log.debug(f'Control input: type={input_type} input={input_init}')
            res = [gr.Tabs.update(selected='out-gallery'), status]
        elif isinstance(selected_init, str): # video via upload > tmp filepath to video
            input_init = selected_init
            input_type = 'gr.Video'
            shared.log.debug(f'Control input: type={input_type} input={input_init}')
            status = get_video(input_init)
            res = [gr.Tabs.update(selected='out-video'), status]
        elif isinstance(selected_init, list): # batch or folder via upload -> list of tmp filepaths
            if hasattr(selected_init[0], 'name'):
                input_type = 'tempfiles'
                input_init = [f.name for f in selected_init] # tempfile
            else:
                input_type = 'files'
                input_init = selected_init
            status = f'Control input | Images | Files {len(input_init)}'
            shared.log.debug(f'Control input: type={input_type} input={input_init}')
            res = [gr.Tabs.update(selected='out-gallery'), status]
        else: # unknown
            input_init = None
    debug(f'Control select input: source={input_source} init={input_init}')
    return res


def video_type_change(video_type):
    return [
        gr.update(visible=video_type != 'None'),
        gr.update(visible=video_type == 'GIF' or video_type == 'PNG'),
        gr.update(visible=video_type == 'MP4'),
        gr.update(visible=video_type == 'MP4'),
    ]


def create_ui(_blocks: gr.Blocks=None):
    initialize()
    if shared.backend == shared.Backend.ORIGINAL:
        with gr.Blocks(analytics_enabled = False) as control_ui:
            pass
        return [(control_ui, 'Control', 'control')]

    with gr.Blocks(analytics_enabled = False) as control_ui:
        prompt, styles, negative, btn_generate, _btn_interrogate, _btn_deepbooru, btn_paste, btn_extra, prompt_counter, btn_prompt_counter, negative_counter, btn_negative_counter  = ui.create_toprow(is_img2img=False, id_part='control')
        with FormGroup(elem_id="control_interface", equal_height=False):
            with gr.Row(elem_id='control_settings'):

                with gr.Accordion(open=False, label="Input", elem_id="control_input", elem_classes=["small-accordion"]):
                    with gr.Row():
                        show_ip = gr.Checkbox(label="Enable IP adapter", value=False, elem_id="control_show_ip")
                    with gr.Row():
                        show_preview = gr.Checkbox(label="Show preview", value=False, elem_id="control_show_preview")
                    with gr.Row():
                        input_type = gr.Radio(label="Input type", choices=['Control only', 'Init image same as control', 'Separate init image'], value='Control only', type='index', elem_id='control_input_type')
                    with gr.Row():
                        denoising_strength = gr.Slider(minimum=0.01, maximum=0.99, step=0.01, label='Denoising strength', value=0.50, elem_id="control_denoising_strength")

                resize_mode, resize_name, width, height, scale_by, selected_scale_tab, resize_time = ui.create_resize_inputs('control', [], time_selector=True, scale_visible=False, mode='Fixed')

                with gr.Accordion(open=False, label="Sampler", elem_id="control_sampler", elem_classes=["small-accordion"]):
                    sd_samplers.set_samplers()
                    steps, sampler_index = ui.create_sampler_and_steps_selection(sd_samplers.samplers, "control")

                batch_count, batch_size = ui.create_batch_inputs('control')
                seed, _reuse_seed, subseed, _reuse_subseed, subseed_strength, seed_resize_from_h, seed_resize_from_w = ui.create_seed_inputs('control', reuse_visible=False)
                cfg_scale, clip_skip, image_cfg_scale, diffusers_guidance_rescale, full_quality, restore_faces, tiling, hdr_clamp, hdr_boundary, hdr_threshold, hdr_center, hdr_channel_shift, hdr_full_shift, hdr_maximize, hdr_max_center, hdr_max_boundry = ui.create_advanced_inputs('control')

                with gr.Accordion(open=False, label="Video", elem_id="control_video", elem_classes=["small-accordion"]):
                    with gr.Row():
                        video_skip_frames = gr.Slider(minimum=0, maximum=100, step=1, label='Skip input frames', value=0, elem_id="control_video_skip_frames")
                    with gr.Row():
                        video_type = gr.Dropdown(label='Video file', choices=['None', 'GIF', 'PNG', 'MP4'], value='None')
                        video_duration = gr.Slider(label='Duration', minimum=0.25, maximum=10, step=0.25, value=2, visible=False)
                    with gr.Row():
                        video_loop = gr.Checkbox(label='Loop', value=True, visible=False)
                        video_pad = gr.Slider(label='Pad frames', minimum=0, maximum=24, step=1, value=1, visible=False)
                        video_interpolate = gr.Slider(label='Interpolate frames', minimum=0, maximum=24, step=1, value=0, visible=False)
                    video_type.change(fn=video_type_change, inputs=[video_type], outputs=[video_duration, video_loop, video_pad, video_interpolate])

                override_settings = ui.create_override_inputs('control')

            with FormRow(variant='compact', elem_id="control_extra_networks", visible=False) as extra_networks_ui:
                from modules import timer, ui_extra_networks
                extra_networks_ui = ui_extra_networks.create_ui(extra_networks_ui, btn_extra, 'control', skip_indexing=shared.opts.extra_network_skip_indexing)
                timer.startup.record('ui-extra-networks')

            with gr.Row(elem_id='control_status'):
                result_txt = gr.HTML(elem_classes=['control-result'], elem_id='control-result')

            with gr.Row(elem_id='control-inputs'):
                with gr.Column(scale=9, elem_id='control-input-column', visible=True) as _column_input:
                    gr.HTML('<span id="control-input-button">Control input</p>')
                    with gr.Tabs(elem_classes=['control-tabs'], elem_id='control-tab-input'):
                        with gr.Tab('Image', id='in-image') as tab_image:
                            input_image = gr.Image(label="Input", show_label=False, type="pil", source="upload", interactive=True, tool="editor", height=gr_height)
                        with gr.Tab('Video', id='in-video') as tab_video:
                            input_video = gr.Video(label="Input", show_label=False, interactive=True, height=gr_height)
                        with gr.Tab('Batch', id='in-batch') as tab_batch:
                            input_batch = gr.File(label="Input", show_label=False, file_count='multiple', file_types=['image'], type='file', interactive=True, height=gr_height)
                        with gr.Tab('Folder', id='in-folder') as tab_folder:
                            input_folder = gr.File(label="Input", show_label=False, file_count='directory', file_types=['image'], type='file', interactive=True, height=gr_height)
                with gr.Column(scale=9, elem_id='control-init-column', visible=False) as column_init:
                    gr.HTML('<span id="control-init-button">Init input</p>')
                    with gr.Tabs(elem_classes=['control-tabs'], elem_id='control-tab-init'):
                        with gr.Tab('Image', id='init-image') as tab_image_init:
                            init_image = gr.Image(label="Input", show_label=False, type="pil", source="upload", interactive=True, tool="editor", height=gr_height)
                        with gr.Tab('Video', id='init-video') as tab_video_init:
                            init_video = gr.Video(label="Input", show_label=False, interactive=True, height=gr_height)
                        with gr.Tab('Batch', id='init-batch') as tab_batch_init:
                            init_batch = gr.File(label="Input", show_label=False, file_count='multiple', file_types=['image'], type='file', interactive=True, height=gr_height)
                        with gr.Tab('Folder', id='init-folder') as tab_folder_init:
                            init_folder = gr.File(label="Input", show_label=False, file_count='directory', file_types=['image'], type='file', interactive=True, height=gr_height)
                with gr.Column(scale=9, elem_id='control-init-column', visible=False) as column_ip:
                    gr.HTML('<span id="control-init-button">IP Adapter</p>')
                    with gr.Tabs(elem_classes=['control-tabs'], elem_id='control-tab-ip'):
                        with gr.Tab('Image', id='init-image') as tab_image_init:
                            ip_image = gr.Image(label="Input", show_label=False, type="pil", source="upload", interactive=True, tool="editor", height=gr_height)
                            with gr.Row():
                                ip_adapter = gr.Dropdown(label='Adapter', choices=ipadapter.ADAPTERS, value='none')
                                ip_scale = gr.Slider(label='Scale', minimum=0.0, maximum=1.0, step=0.01, value=0.5)
                            with gr.Row():
                                ip_type = gr.Radio(label="Input type", choices=['Init image same as control', 'Separate init image'], value='Init image same as control', type='index', elem_id='control_ip_type')
                            ip_image.change(fn=lambda x: gr.update(value='Init image same as control' if x is None else 'Separate init image'), inputs=[ip_image], outputs=[ip_type])
                with gr.Column(scale=9, elem_id='control-output-column', visible=True) as _column_output:
                    gr.HTML('<span id="control-output-button">Output</p>')
                    with gr.Tabs(elem_classes=['control-tabs'], elem_id='control-tab-output') as output_tabs:
                        with gr.Tab('Gallery', id='out-gallery'):
                            output_gallery, _output_gen_info, _output_html_info, _output_html_info_formatted, _output_html_log = ui_common.create_output_panel("control", preview=True)
                        with gr.Tab('Image', id='out-image'):
                            output_image = gr.Image(label="Input", show_label=False, type="pil", interactive=False, tool="editor", height=gr_height)
                        with gr.Tab('Video', id='out-video'):
                            output_video = gr.Video(label="Input", show_label=False, height=gr_height)
                with gr.Column(scale=9, elem_id='control-preview-column', visible=False) as column_preview:
                    gr.HTML('<span id="control-preview-button">Preview</p>')
                    with gr.Tabs(elem_classes=['control-tabs'], elem_id='control-tab-preview'):
                        with gr.Tab('Preview', id='preview-image') as tab_image:
                            preview_process = gr.Image(label="Input", show_label=False, type="pil", source="upload", interactive=False, height=gr_height, visible=True)

                for ctrl in [input_image, input_video, input_batch, input_folder, init_image, init_video, init_batch, init_folder, tab_image, tab_video, tab_batch, tab_folder, tab_image_init, tab_video_init, tab_batch_init, tab_folder_init]:
                    inputs = [input_image, init_image, input_type]
                    outputs = [output_tabs, result_txt]
                    if hasattr(ctrl, 'change'):
                        ctrl.change(fn=select_input, inputs=inputs, outputs=outputs)
                    if hasattr(ctrl, 'select'):
                        ctrl.select(fn=select_input, inputs=inputs, outputs=outputs)
                show_preview.change(fn=lambda x: gr.update(visible=x), inputs=[show_preview], outputs=[column_preview])
                show_ip.change(fn=lambda x: gr.update(visible=x), inputs=[show_ip], outputs=[column_ip])
                input_type.change(fn=lambda x: gr.update(visible=x == 2), inputs=[input_type], outputs=[column_init])

            with gr.Tabs(elem_id='control-tabs') as _tabs_control_type:

                with gr.Tab('ControlNet') as _tab_controlnet:
                    gr.HTML('<a href="https://github.com/lllyasviel/ControlNet">ControlNet</a>')
                    with gr.Row():
                        extra_controls = [
                            gr.Checkbox(label="Guess mode", value=False, scale=3),
                        ]
                        num_controlnet_units = gr.Slider(label="Units", minimum=1, maximum=max_units, step=1, value=1, scale=1)
                    controlnet_ui_units = [] # list of hidable accordions
                    for i in range(max_units):
                        with gr.Accordion(f'Control unit {i+1}', visible= i < num_controlnet_units.value) as unit_ui:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        enabled_cb = gr.Checkbox(value= i==0, label="")
                                        process_id = gr.Dropdown(label="Processor", choices=processors.list_models(), value='None')
                                        model_id = gr.Dropdown(label="ControlNet", choices=controlnet.list_models(), value='None')
                                        ui_common.create_refresh_button(model_id, controlnet.list_models, lambda: {"choices": controlnet.list_models(refresh=True)}, 'refresh_control_models')
                                        model_strength = gr.Slider(label="Strength", minimum=0.01, maximum=1.0, step=0.01, value=1.0-i/10)
                                        control_start = gr.Slider(label="Start", minimum=0.0, maximum=1.0, step=0.05, value=0)
                                        control_end = gr.Slider(label="End", minimum=0.0, maximum=1.0, step=0.05, value=1.0)
                                        reset_btn = ui_components.ToolButton(value=ui_symbols.reset)
                                        image_upload = gr.UploadButton(label=ui_symbols.upload, file_types=['image'], elem_classes=['form', 'gradio-button', 'tool'])
                                        process_btn= ui_components.ToolButton(value=ui_symbols.preview)
                        controlnet_ui_units.append(unit_ui)
                        units.append(unit.Unit(
                            unit_type = 'controlnet',
                            result_txt = result_txt,
                            image_input = input_image,
                            enabled_cb = enabled_cb,
                            reset_btn = reset_btn,
                            process_id = process_id,
                            model_id = model_id,
                            model_strength = model_strength,
                            preview_process = preview_process,
                            preview_btn = process_btn,
                            image_upload = image_upload,
                            control_start = control_start,
                            control_end = control_end,
                            extra_controls = extra_controls,
                            )
                        )
                        if i == 0:
                            units[-1].enabled = True # enable first unit in group
                    num_controlnet_units.change(fn=display_units, inputs=[num_controlnet_units], outputs=controlnet_ui_units)

                with gr.Tab('XS') as _tab_controlnetxs:
                    gr.HTML('<a href="https://vislearn.github.io/ControlNet-XS/">ControlNet XS</a>')
                    with gr.Row():
                        extra_controls = [
                            gr.Slider(label="Time embedding mix", minimum=0.0, maximum=1.0, step=0.05, value=0.0, scale=3)
                        ]
                        num_controlnet_units = gr.Slider(label="Units", minimum=1, maximum=max_units, step=1, value=1, scale=1)
                    controlnetxs_ui_units = [] # list of hidable accordions
                    for i in range(max_units):
                        with gr.Accordion(f'Control unit {i+1}', visible= i < num_controlnet_units.value) as unit_ui:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        enabled_cb = gr.Checkbox(value= i==0, label="")
                                        process_id = gr.Dropdown(label="Processor", choices=processors.list_models(), value='None')
                                        model_id = gr.Dropdown(label="ControlNet-XS", choices=xs.list_models(), value='None')
                                        ui_common.create_refresh_button(model_id, xs.list_models, lambda: {"choices": xs.list_models(refresh=True)}, 'refresh_control_models')
                                        model_strength = gr.Slider(label="Strength", minimum=0.01, maximum=1.0, step=0.01, value=1.0-i/10)
                                        control_start = gr.Slider(label="Start", minimum=0.0, maximum=1.0, step=0.05, value=0)
                                        control_end = gr.Slider(label="End", minimum=0.0, maximum=1.0, step=0.05, value=1.0)
                                        reset_btn = ui_components.ToolButton(value=ui_symbols.reset)
                                        image_upload = gr.UploadButton(label=ui_symbols.upload, file_types=['image'], elem_classes=['form', 'gradio-button', 'tool'])
                                        process_btn= ui_components.ToolButton(value=ui_symbols.preview)
                        controlnetxs_ui_units.append(unit_ui)
                        units.append(unit.Unit(
                            unit_type = 'xs',
                            result_txt = result_txt,
                            image_input = input_image,
                            enabled_cb = enabled_cb,
                            reset_btn = reset_btn,
                            process_id = process_id,
                            model_id = model_id,
                            model_strength = model_strength,
                            preview_process = preview_process,
                            preview_btn = process_btn,
                            image_upload = image_upload,
                            control_start = control_start,
                            control_end = control_end,
                            extra_controls = extra_controls,
                            )
                        )
                        if i == 0:
                            units[-1].enabled = True # enable first unit in group
                    num_controlnet_units.change(fn=display_units, inputs=[num_controlnet_units], outputs=controlnetxs_ui_units)

                with gr.Tab('Adapter') as _tab_adapter:
                    gr.HTML('<a href="https://github.com/TencentARC/T2I-Adapter">T2I-Adapter</a>')
                    with gr.Row():
                        extra_controls = [
                            gr.Slider(label="Control factor", minimum=0.0, maximum=1.0, step=0.05, value=1.0, scale=3),
                        ]
                        num_adapter_units = gr.Slider(label="Units", minimum=1, maximum=max_units, step=1, value=1, scale=1)
                    adapter_ui_units = [] # list of hidable accordions
                    for i in range(max_units):
                        with gr.Accordion(f'Adapter unit {i+1}', visible= i < num_adapter_units.value) as unit_ui:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        enabled_cb = gr.Checkbox(value= i == 0, label="Enabled")
                                        process_id = gr.Dropdown(label="Processor", choices=processors.list_models(), value='None')
                                        model_id = gr.Dropdown(label="Adapter", choices=t2iadapter.list_models(), value='None')
                                        ui_common.create_refresh_button(model_id, t2iadapter.list_models, lambda: {"choices": t2iadapter.list_models(refresh=True)}, 'refresh_adapter_models')
                                        model_strength = gr.Slider(label="Strength", minimum=0.01, maximum=1.0, step=0.01, value=1.0-i/10)
                                        reset_btn = ui_components.ToolButton(value=ui_symbols.reset)
                                        image_upload = gr.UploadButton(label=ui_symbols.upload, file_types=['image'], elem_classes=['form', 'gradio-button', 'tool'])
                                        process_btn= ui_components.ToolButton(value=ui_symbols.preview)
                        adapter_ui_units.append(unit_ui)
                        units.append(unit.Unit(
                            unit_type = 'adapter',
                            result_txt = result_txt,
                            image_input = input_image,
                            enabled_cb = enabled_cb,
                            reset_btn = reset_btn,
                            process_id = process_id,
                            model_id = model_id,
                            model_strength = model_strength,
                            preview_process = preview_process,
                            preview_btn = process_btn,
                            image_upload = image_upload,
                            extra_controls = extra_controls,
                            )
                        )
                        if i == 0:
                            units[-1].enabled = True # enable first unit in group
                    num_adapter_units.change(fn=display_units, inputs=[num_adapter_units], outputs=adapter_ui_units)

                with gr.Tab('Lite') as _tab_lite:
                    gr.HTML('<a href="https://huggingface.co/kohya-ss/controlnet-lllite">Control LLLite</a>')
                    with gr.Row():
                        extra_controls = [
                        ]
                        num_lite_units = gr.Slider(label="Units", minimum=1, maximum=max_units, step=1, value=1, scale=1)
                    lite_ui_units = [] # list of hidable accordions
                    for i in range(max_units):
                        with gr.Accordion(f'Control unit {i+1}', visible= i < num_lite_units.value) as unit_ui:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        enabled_cb = gr.Checkbox(value= i == 0, label="Enabled")
                                        process_id = gr.Dropdown(label="Processor", choices=processors.list_models(), value='None')
                                        model_id = gr.Dropdown(label="Model", choices=lite.list_models(), value='None')
                                        ui_common.create_refresh_button(model_id, lite.list_models, lambda: {"choices": lite.list_models(refresh=True)}, 'refresh_lite_models')
                                        model_strength = gr.Slider(label="Strength", minimum=0.01, maximum=1.0, step=0.01, value=1.0-i/10)
                                        reset_btn = ui_components.ToolButton(value=ui_symbols.reset)
                                        image_upload = gr.UploadButton(label=ui_symbols.upload, file_types=['image'], elem_classes=['form', 'gradio-button', 'tool'])
                                        process_btn= ui_components.ToolButton(value=ui_symbols.preview)
                        lite_ui_units.append(unit_ui)
                        units.append(unit.Unit(
                            unit_type = 'lite',
                            result_txt = result_txt,
                            image_input = input_image,
                            enabled_cb = enabled_cb,
                            reset_btn = reset_btn,
                            process_id = process_id,
                            model_id = model_id,
                            model_strength = model_strength,
                            preview_process = preview_process,
                            preview_btn = process_btn,
                            image_upload = image_upload,
                            extra_controls = extra_controls,
                            )
                        )
                        if i == 0:
                            units[-1].enabled = True # enable first unit in group
                    num_lite_units.change(fn=display_units, inputs=[num_lite_units], outputs=lite_ui_units)

                with gr.Tab('Reference') as _tab_reference:
                    gr.HTML('<a href="https://github.com/Mikubill/sd-webui-controlnet/discussions/1236">ControlNet reference-only control</a>')
                    with gr.Row():
                        extra_controls = [
                            gr.Radio(label="Reference context", choices=['Attention', 'Adain', 'Attention Adain'], value='Attention', interactive=True),
                            gr.Slider(label="Style fidelity", minimum=0.0, maximum=1.0, step=0.05, value=0.5, interactive=True), # prompt vs control importance
                            gr.Slider(label="Reference query weight", minimum=0.0, maximum=1.0, step=0.05, value=1.0, interactive=True),
                            gr.Slider(label="Reference adain weight", minimum=0.0, maximum=2.0, step=0.05, value=1.0, interactive=True),
                        ]
                    for i in range(1): # can only have one reference unit
                        with gr.Accordion(f'Reference unit {i+1}', visible=True) as unit_ui:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        enabled_cb = gr.Checkbox(value= i == 0, label="Enabled", visible=False)
                                        model_id = gr.Dropdown(label="Reference", choices=reference.list_models(), value='Reference', visible=False)
                                        model_strength = gr.Slider(label="Strength", minimum=0.01, maximum=1.0, step=0.01, value=1.0, visible=False)
                                        reset_btn = ui_components.ToolButton(value=ui_symbols.reset)
                                        image_upload = gr.UploadButton(label=ui_symbols.upload, file_types=['image'], elem_classes=['form', 'gradio-button', 'tool'])
                                        process_btn= ui_components.ToolButton(value=ui_symbols.preview)
                        units.append(unit.Unit(
                            unit_type = 'reference',
                            result_txt = result_txt,
                            image_input = input_image,
                            enabled_cb = enabled_cb,
                            reset_btn = reset_btn,
                            process_id = process_id,
                            model_id = model_id,
                            model_strength = model_strength,
                            preview_process = preview_process,
                            preview_btn = process_btn,
                            image_upload = image_upload,
                            extra_controls = extra_controls,
                            )
                        )
                        if i == 0:
                            units[-1].enabled = True # enable first unit in group

                with gr.Tab('Processor settings') as _tab_settings:
                    with gr.Group(elem_classes=['processor-group']):
                        settings = []
                        with gr.Accordion('HED', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Scribble", value=False))
                        with gr.Accordion('Midas depth', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Slider(label="Background threshold", minimum=0.0, maximum=1.0, step=0.01, value=0.1))
                            settings.append(gr.Checkbox(label="Depth and normal", value=False))
                        with gr.Accordion('MLSD', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Slider(label="Score threshold", minimum=0.0, maximum=1.0, step=0.01, value=0.1))
                            settings.append(gr.Slider(label="Distance threshold", minimum=0.0, maximum=1.0, step=0.01, value=0.1))
                        with gr.Accordion('OpenBody', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Body", value=True))
                            settings.append(gr.Checkbox(label="Hands", value=False))
                            settings.append(gr.Checkbox(label="Face", value=False))
                        with gr.Accordion('PidiNet', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Scribble", value=False))
                            settings.append(gr.Checkbox(label="Apply filter", value=False))
                        with gr.Accordion('LineArt', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Coarse", value=False))
                        with gr.Accordion('Leres Depth', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Boost", value=False))
                            settings.append(gr.Slider(label="Near threshold", minimum=0.0, maximum=1.0, step=0.01, value=0.0))
                            settings.append(gr.Slider(label="Background threshold", minimum=0.0, maximum=1.0, step=0.01, value=0.0))
                        with gr.Accordion('MediaPipe Face', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Slider(label="Max faces", minimum=1, maximum=10, step=1, value=1))
                            settings.append(gr.Slider(label="Min confidence", minimum=0.0, maximum=1.0, step=0.01, value=0.5))
                        with gr.Accordion('Canny', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Slider(label="Low threshold", minimum=0, maximum=1000, step=1, value=100))
                            settings.append(gr.Slider(label="High threshold", minimum=0, maximum=1000, step=1, value=200))
                        with gr.Accordion('DWPose', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Radio(label="Model", choices=['Tiny', 'Medium', 'Large'], value='Tiny'))
                            settings.append(gr.Slider(label="Min confidence", minimum=0.0, maximum=1.0, step=0.01, value=0.3))
                        with gr.Accordion('SegmentAnything', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Radio(label="Model", choices=['Base', 'Large'], value='Base'))
                        with gr.Accordion('Edge', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Parameter free", value=True))
                            settings.append(gr.Radio(label="Mode", choices=['edge', 'gradient'], value='edge'))
                        with gr.Accordion('Zoe Depth', open=True, elem_classes=['processor-settings']):
                            settings.append(gr.Checkbox(label="Gamma corrected", value=False))
                        for setting in settings:
                            setting.change(fn=processors.update_settings, inputs=settings, outputs=[])

                tabs_state = gr.Text(value='none', visible=False)
                input_fields = [
                    input_type,
                    prompt, negative, styles,
                    steps, sampler_index,
                    seed, subseed, subseed_strength, seed_resize_from_h, seed_resize_from_w,
                    cfg_scale, clip_skip, image_cfg_scale, diffusers_guidance_rescale, full_quality, restore_faces, tiling, hdr_clamp, hdr_boundary, hdr_threshold, hdr_center, hdr_channel_shift, hdr_full_shift, hdr_maximize, hdr_max_center, hdr_max_boundry,
                    resize_mode, resize_name, width, height, scale_by, selected_scale_tab, resize_time,
                    denoising_strength, batch_count, batch_size,
                    video_skip_frames, video_type, video_duration, video_loop, video_pad, video_interpolate,
                    ip_adapter, ip_scale, ip_image, ip_type,
                ]
                output_fields = [
                    preview_process,
                    output_image,
                    output_video,
                    output_gallery,
                    result_txt,
                ]
                paste_fields = [] # TODO paste fields

                control_dict = dict(
                    fn=generate_click,
                    _js="submit_control",
                    inputs=[tabs_state, tabs_state] + input_fields,
                    outputs=output_fields,
                    show_progress=False,
                )
                prompt.submit(**control_dict)
                btn_generate.click(**control_dict)

                btn_prompt_counter.click(fn=call_queue.wrap_queued_call(ui.update_token_counter), inputs=[prompt, steps], outputs=[prompt_counter])
                btn_negative_counter.click(fn=call_queue.wrap_queued_call(ui.update_token_counter), inputs=[negative, steps], outputs=[negative_counter])

                generation_parameters_copypaste.add_paste_fields("control", input_image, paste_fields, override_settings)
                bindings = generation_parameters_copypaste.ParamBinding(paste_button=btn_paste, tabname="control", source_text_component=prompt, source_image_component=output_gallery)
                generation_parameters_copypaste.register_paste_params_button(bindings)

                if os.environ.get('SD_CONTROL_DEBUG', None) is not None: # debug only
                    from modules.control.test import test_processors, test_controlnets, test_adapters, test_xs, test_lite
                    gr.HTML('<br><h1>Debug</h1><br>')
                    with gr.Row():
                        run_test_processors_btn = gr.Button(value="Test:Processors", variant='primary', elem_classes=['control-button'])
                        run_test_controlnets_btn = gr.Button(value="Test:ControlNets", variant='primary', elem_classes=['control-button'])
                        run_test_xs_btn = gr.Button(value="Test:ControlNets-XS", variant='primary', elem_classes=['control-button'])
                        run_test_adapters_btn = gr.Button(value="Test:Adapters", variant='primary', elem_classes=['control-button'])
                        run_test_lite_btn = gr.Button(value="Test:Control-LLLite", variant='primary', elem_classes=['control-button'])

                        run_test_processors_btn.click(fn=test_processors, inputs=[input_image], outputs=[preview_process, output_image, output_video, output_gallery])
                        run_test_controlnets_btn.click(fn=test_controlnets, inputs=[prompt, negative, input_image], outputs=[preview_process, output_image, output_video, output_gallery])
                        run_test_xs_btn.click(fn=test_xs, inputs=[prompt, negative, input_image], outputs=[preview_process, output_image, output_video, output_gallery])
                        run_test_adapters_btn.click(fn=test_adapters, inputs=[prompt, negative, input_image], outputs=[preview_process, output_image, output_video, output_gallery])
                        run_test_lite_btn.click(fn=test_lite, inputs=[prompt, negative, input_image], outputs=[preview_process, output_image, output_video, output_gallery])

    return [(control_ui, 'Control', 'control')]
