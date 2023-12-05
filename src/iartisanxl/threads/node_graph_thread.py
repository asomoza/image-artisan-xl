import os
import logging

import torch
from PIL import Image

from PyQt6.QtCore import QThread, pyqtSignal

from iartisanxl.app.directories import DirectoriesObject
from iartisanxl.generation.image_generation_data import ImageGenerationData
from iartisanxl.generation.lora_list import LoraList
from iartisanxl.generation.controlnet_list import ControlNetList
from iartisanxl.generation.t2i_adapter_list import T2IAdapterList
from iartisanxl.graph.iartisanxl_node_graph import ImageArtisanNodeGraph
from iartisanxl.graph.nodes.lora_node import LoraNode
from iartisanxl.graph.nodes.controlnet_model_node import ControlnetModelNode
from iartisanxl.graph.nodes.controlnet_node import ControlnetNode
from iartisanxl.graph.nodes.t2i_adapter_model_node import T2IAdapterModelNode
from iartisanxl.graph.nodes.t2i_adapter_node import T2IAdapterNode
from iartisanxl.graph.nodes.image_load_node import ImageLoadNode


class NodeGraphThread(QThread):
    status_changed = pyqtSignal(str)
    progress_update = pyqtSignal(int, torch.Tensor)
    generation_finished = pyqtSignal(Image.Image)
    generation_error = pyqtSignal(str, bool)
    generation_aborted = pyqtSignal()

    def __init__(
        self,
        directories: DirectoriesObject = None,
        node_graph: ImageArtisanNodeGraph = None,
        image_generation_data: ImageGenerationData = None,
        lora_list: LoraList = None,
        controlnet_list: ControlNetList = None,
        t2i_adapter_list: T2IAdapterList = None,
        model_offload: bool = False,
        sequential_offload: bool = False,
        torch_dtype: torch.dtype = torch.float16,
    ):
        super().__init__()
        self.logger = logging.getLogger()
        self.directories = directories
        self.node_graph = node_graph
        self.image_generation_data = image_generation_data
        self.lora_list = lora_list
        self.controlnet_list = controlnet_list
        self.t2i_adapter_list = t2i_adapter_list
        self.model_offload = model_offload
        self.sequential_offload = sequential_offload
        self.torch_dtype = torch_dtype

    def run(self):
        self.status_changed.emit("Generating image...")

        if self.node_graph is None:
            self.node_graph = self.image_generation_data.create_text_to_image_graph()

            # connect the essential callbacks
            self.node_graph.set_abort_function(self.on_aborted)
            image_generation = self.node_graph.get_node_by_name("image_generation")
            image_generation.callback = self.step_progress_update

            send_node = self.node_graph.get_node_by_name("image_send")
            send_node.image_callback = self.preview_image
        else:
            changed = self.image_generation_data.get_changed_attributes()
            for attr_name, new_value in changed.items():
                node = self.node_graph.get_node_by_name(attr_name)

                if attr_name == "model":
                    node.update_model(path=new_value["path"], model_name=new_value["name"], version=new_value["version"], model_type=new_value["type"])
                elif attr_name == "vae":
                    node.update_model(path=new_value["path"], vae_name=new_value["name"])
                else:
                    node.update_value(new_value)

            self.image_generation_data.update_previous_state()

        if self.node_graph.sequential_offload != self.sequential_offload:
            self.check_and_update("sequential_offload", "sequential_offload", self.sequential_offload)
        elif self.node_graph.cpu_offload != self.model_offload:
            self.check_and_update("cpu_offload", "model_offload", self.model_offload)

        sdxl_model = self.node_graph.get_node_by_name("model")
        prompts_encoder = self.node_graph.get_node_by_name("prompts_encoder")
        image_generation = self.node_graph.get_node_by_name("image_generation")
        decoder = self.node_graph.get_node_by_name("decoder")
        image_send = self.node_graph.get_node_by_name("image_send")

        # process loras
        if len(self.lora_list.loras) > 0:
            lora_scale = self.node_graph.get_node_by_name("lora_scale")

            # if there's a image dropped to generate, reset all the loras since its impossible to keep track of the ids for the nodes
            if self.lora_list.dropped_image:
                sdxl_model.unload_lora_weights()
                lora_nodes = self.node_graph.get_all_nodes_class(LoraNode)
                for lora_node in lora_nodes:
                    self.node_graph.delete_node_by_id(lora_node.id)

                new_loras = self.lora_list.loras

                for lora in new_loras:
                    lora_node = LoraNode(path=lora.path, adapter_name=lora.filename, scale=lora.weight, lora_name=lora.name, version=lora.version)
                    lora_node.connect("unet", sdxl_model, "unet")
                    lora_node.connect("text_encoder_1", sdxl_model, "text_encoder_1")
                    lora_node.connect("text_encoder_2", sdxl_model, "text_encoder_2")
                    lora_node.connect("global_lora_scale", lora_scale, "value")
                    self.node_graph.add_node(lora_node, lora.filename)
                    lora.id = lora_node.id
                    image_generation.connect("lora", lora_node, "lora")

                    # this is manually updated since it doesn't have a relation with the node (add a system for this)
                    prompts_encoder.updated = True

                    # ugly patch while I find why they dont get flagged as updated
                    decoder.updated = True
                    image_send.updated = True

            else:
                new_loras = self.lora_list.get_added()

                if len(new_loras) > 0:
                    for lora in new_loras:
                        lora_node = LoraNode(path=lora.path, adapter_name=lora.filename, scale=lora.weight, lora_name=lora.name, version=lora.version)
                        lora_node.connect("unet", sdxl_model, "unet")
                        lora_node.connect("text_encoder_1", sdxl_model, "text_encoder_1")
                        lora_node.connect("text_encoder_2", sdxl_model, "text_encoder_2")
                        lora_node.connect("global_lora_scale", lora_scale, "value")
                        self.node_graph.add_node(lora_node, lora.filename)
                        lora.id = lora_node.id
                        image_generation.connect("lora", lora_node, "lora")

                        # this is manually updated since it doesn't have a relation with the node (add a system for this)
                        prompts_encoder.updated = True

                        # ugly patch while I find why they dont get flagged as updated
                        decoder.updated = True
                        image_send.updated = True

                modified_loras = self.lora_list.get_modified()

                if len(modified_loras) > 0:
                    for lora in modified_loras:
                        lora_node = self.node_graph.get_node(lora.id)
                        lora_node.update_lora(lora.weight, lora.enabled)

                        # same as before
                        prompts_encoder.updated = True
                        decoder.updated = True
                        image_send.updated = True

                removed_loras = self.lora_list.get_removed()

                if len(removed_loras) > 0:
                    adapter_names = []
                    for lora in removed_loras:
                        self.node_graph.delete_node_by_id(lora.id)
                        adapter_names.append(lora.filename)

                    if len(adapter_names) > 0:
                        sdxl_model.delete_adapters(adapter_names)

                    # same as before
                    prompts_encoder.updated = True
                    decoder.updated = True
                    image_send.updated = True
        else:
            removed_loras = self.lora_list.get_removed()

            if len(removed_loras) > 0:
                for lora in removed_loras:
                    self.node_graph.delete_node_by_id(lora.id)

                sdxl_model.unload_lora_weights()

                # same as before
                prompts_encoder.updated = True
                decoder.updated = True
                image_send.updated = True

        self.lora_list.save_state()
        self.lora_list.dropped_image = False

        # process controlnets
        controlnet_types = self.controlnet_list.get_used_types()
        controlnet_canny_model = None
        controlnet_depth_model = None
        controlnet_pose_model = None

        for controlnet_type in controlnet_types:
            if controlnet_type == "Canny":
                controlnet_canny_model = self.node_graph.get_node_by_name("controlnet_canny_model")

                if controlnet_canny_model is None:
                    controlnet_canny_model = ControlnetModelNode(path=os.path.join(self.directories.models_controlnets, "controlnet-canny-sdxl-1.0-small"))
                    self.node_graph.add_node(controlnet_canny_model, "controlnet_canny_model")
            elif controlnet_type == "Depth Midas":
                controlnet_depth_model = self.node_graph.get_node_by_name("controlnet_depth_model")

                if controlnet_depth_model is None:
                    controlnet_depth_model = ControlnetModelNode(path=os.path.join(self.directories.models_controlnets, "controlnet-depth-sdxl-1.0-small"))
                    self.node_graph.add_node(controlnet_depth_model, "controlnet_depth_model")
            elif controlnet_type == "Depth Zoe":
                controlnet_depth_zoe_model = self.node_graph.get_node_by_name("controlnet_depth_zoe_model")

                if controlnet_depth_zoe_model is None:
                    controlnet_depth_zoe_model = ControlnetModelNode(
                        path=os.path.join(self.directories.models_controlnets, "controlnet-zoe-depth-sdxl-1.0")
                    )
                    self.node_graph.add_node(controlnet_depth_zoe_model, "controlnet_depth_zoe_model")
            elif controlnet_type == "Pose":
                controlnet_pose_model = self.node_graph.get_node_by_name("controlnet_pose_model")

                if controlnet_pose_model is None:
                    controlnet_pose_model = ControlnetModelNode(path=os.path.join(self.directories.models_controlnets, "controlnet-openpose-sdxl-1.0"))
                    self.node_graph.add_node(controlnet_pose_model, "controlnet_pose_model")

        if len(self.controlnet_list.controlnets) > 0:
            added_controlnets = self.controlnet_list.get_added()

            if len(added_controlnets) > 0:
                for controlnet in added_controlnets:
                    controlnet_image_node = ImageLoadNode(image=controlnet.annotator_image)
                    controlnet_node = ControlnetNode(
                        conditioning_scale=controlnet.conditioning_scale, guidance_start=controlnet.guidance_start, guidance_end=controlnet.guidance_end
                    )

                    if controlnet.controlnet_type == "Canny":
                        controlnet_node.connect("controlnet_model", controlnet_canny_model, "controlnet_model")
                    elif controlnet.controlnet_type == "Depth Midas":
                        controlnet_node.connect("controlnet_model", controlnet_depth_model, "controlnet_model")
                    elif controlnet.controlnet_type == "Depth Zoe":
                        controlnet_node.connect("controlnet_model", controlnet_depth_zoe_model, "controlnet_model")
                    elif controlnet.controlnet_type == "Pose":
                        controlnet_node.connect("controlnet_model", controlnet_pose_model, "controlnet_model")

                    controlnet_node.connect("image", controlnet_image_node, "image")
                    image_generation.connect("controlnet", controlnet_node, "controlnet")
                    self.node_graph.add_node(controlnet_node)
                    controlnet.id = controlnet_node.id
                    controlnet_node.name = f"controlnet_{controlnet.controlnet_type}_{controlnet_node.id}"
                    self.node_graph.add_node(controlnet_image_node, f"control_image_{controlnet_node.id}")

                image_send.updated = True

            modified_controlnets = self.controlnet_list.get_modified()

            if len(modified_controlnets) > 0:
                for controlnet in modified_controlnets:
                    control_image_node = self.node_graph.get_node_by_name(f"control_image_{controlnet.id}")
                    control_image_node.update_image(controlnet.annotator_image)
                    controlnet_node = self.node_graph.get_node(controlnet.id)
                    controlnet_node.update_controlnet(
                        controlnet.conditioning_scale, controlnet.guidance_start, controlnet.guidance_end, controlnet.enabled
                    )
                image_send.updated = True

        removed_controlnets = self.controlnet_list.get_removed()
        if len(removed_controlnets) > 0:
            for controlnet in removed_controlnets:
                control_image_node = self.node_graph.get_node_by_name(f"control_image_{controlnet.id}")
                self.node_graph.delete_node_by_id(control_image_node.id)
                self.node_graph.delete_node_by_id(controlnet.id)

        self.controlnet_list.save_state()
        self.controlnet_list.dropped_image = False

        # process t2i_adapters
        t2i_adapter_types = self.t2i_adapter_list.get_used_types()

        for t2i_adapter_type in t2i_adapter_types:
            print(f"{t2i_adapter_type=}")
            if t2i_adapter_type == "canny":
                t2i_adapter_canny_model = self.node_graph.get_node_by_name("t2i_adapter_canny_model")

                if t2i_adapter_canny_model is None:
                    t2i_adapter_canny_model = T2IAdapterModelNode(path=os.path.join(self.directories.models_t2i_adapters, "t2i-adapter-canny-sdxl-1.0"))
                    self.node_graph.add_node(t2i_adapter_canny_model, "t2i_adapter_canny_model")
            elif t2i_adapter_type == "depth":
                t2i_adapter_depth_model = self.node_graph.get_node_by_name("t2i_adapter_depth_model")

                if t2i_adapter_depth_model is None:
                    t2i_adapter_depth_model = T2IAdapterModelNode(
                        path=os.path.join(self.directories.models_t2i_adapters, "t2i-adapter-depth-midas-sdxl-1.0")
                    )
                    self.node_graph.add_node(t2i_adapter_depth_model, "t2i_adapter_depth_model")
            elif t2i_adapter_type == "pose":
                t2i_adapter_pose_model = self.node_graph.get_node_by_name("t2i_adapter_pose_model")

                if t2i_adapter_pose_model is None:
                    t2i_adapter_pose_model = T2IAdapterModelNode(path=os.path.join(self.directories.models_t2i_adapters, "t2i-adapter-openpose-sdxl-1.0"))
                    self.node_graph.add_node(t2i_adapter_pose_model, "t2i_adapter_pose_model")
            elif t2i_adapter_type == "lineart":
                t2i_adapter_lineart_model = self.node_graph.get_node_by_name("t2i_adapter_lineart_model")

                if t2i_adapter_lineart_model is None:
                    t2i_adapter_lineart_model = T2IAdapterModelNode(
                        path=os.path.join(self.directories.models_t2i_adapters, "t2i-adapter-lineart-sdxl-1.0")
                    )
                    self.node_graph.add_node(t2i_adapter_lineart_model, "t2i_adapter_lineart_model")
            elif t2i_adapter_type == "sketch":
                t2i_adapter_sketch_model = self.node_graph.get_node_by_name("t2i_adapter_sketch_model")

                if t2i_adapter_sketch_model is None:
                    t2i_adapter_sketch_model = T2IAdapterModelNode(path=os.path.join(self.directories.models_t2i_adapters, "t2i-adapter-sketch-sdxl-1.0"))
                    self.node_graph.add_node(t2i_adapter_sketch_model, "t2i_adapter_sketch_model")

        if len(self.t2i_adapter_list.adapters) > 0:
            added_t2i_adapters = self.t2i_adapter_list.get_added()

            if len(added_t2i_adapters) > 0:
                for t2i_adapter in added_t2i_adapters:
                    t2i_adapter_image_node = ImageLoadNode(image=t2i_adapter.annotator_image)
                    t2i_adapter_node = T2IAdapterNode(
                        conditioning_scale=t2i_adapter.conditioning_scale, conditioning_factor=t2i_adapter.conditioning_factor
                    )

                    if t2i_adapter.adapter_type == "canny":
                        t2i_adapter_node.connect("t2i_adapter_model", t2i_adapter_canny_model, "t2i_adapter_model")
                    elif t2i_adapter.adapter_type == "depth":
                        t2i_adapter_node.connect("t2i_adapter_model", t2i_adapter_depth_model, "t2i_adapter_model")
                    elif t2i_adapter.adapter_type == "pose":
                        t2i_adapter_node.connect("t2i_adapter_model", t2i_adapter_pose_model, "t2i_adapter_model")
                    elif t2i_adapter.adapter_type == "lineart":
                        t2i_adapter_node.connect("t2i_adapter_model", t2i_adapter_lineart_model, "t2i_adapter_model")
                    elif t2i_adapter.adapter_type == "sketch":
                        t2i_adapter_node.connect("t2i_adapter_model", t2i_adapter_sketch_model, "t2i_adapter_model")

                    t2i_adapter_node.connect("image", t2i_adapter_image_node, "image")
                    image_generation.connect("t2i_adapter", t2i_adapter_node, "t2i_adapter")
                    self.node_graph.add_node(t2i_adapter_node)
                    t2i_adapter.id = t2i_adapter_node.id
                    self.node_graph.add_node(t2i_adapter_image_node, f"adapter_image_{t2i_adapter_node.id}")

                image_send.updated = True

            modified_t2i_adapters = self.t2i_adapter_list.get_modified()

            if len(modified_t2i_adapters) > 0:
                for t2i_adapter in modified_t2i_adapters:
                    t2i_adapter_image_node = self.node_graph.get_node_by_name(f"adapter_image_{t2i_adapter.id}")
                    t2i_adapter_image_node.update_image(t2i_adapter.annotator_image)
                    t2i_adapter_node = self.node_graph.get_node(t2i_adapter.id)
                    t2i_adapter_node.update_adaptert(t2i_adapter.conditioning_scale, t2i_adapter.conditioning_factor, t2i_adapter.enabled)

                image_send.updated = True

        removed_t2i_adapters = self.t2i_adapter_list.get_removed()
        if len(removed_t2i_adapters) > 0:
            for t2i_adapter in removed_t2i_adapters:
                adapter_image_node = self.node_graph.get_node_by_name(f"adapter_image_{t2i_adapter.id}")
                self.node_graph.delete_node_by_id(adapter_image_node.id)
                self.node_graph.delete_node_by_id(t2i_adapter.id)

        self.t2i_adapter_list.save_state()
        self.t2i_adapter_list.dropped_image = False

        try:
            self.node_graph()
        except KeyError:
            self.generation_error.emit("There was an error while generating.", False)
        except FileNotFoundError:
            self.generation_error.emit("There's a missing model file in the generation.", False)

        if not self.node_graph.updated:
            self.generation_error.emit("Nothing was changed", False)

    def step_progress_update(self, step, _timestep, latents):
        self.progress_update.emit(step, latents)

    def preview_image(self, image):
        self.generation_finished.emit(image)

    def reset_model_path(self, model_name):
        model_node = self.node_graph.get_node_by_name(model_name)
        if model_node is not None:
            model_node.set_updated()

    def check_and_update(self, attr1, attr2, value):
        if getattr(self.node_graph, attr1) != getattr(self, attr2):
            self.reset_model_path("model")
            self.reset_model_path("vae_model")
            setattr(self.node_graph, attr1, value)

    def abort_graph(self):
        self.node_graph.abort_graph()

    def on_aborted(self):
        self.generation_aborted.emit()
