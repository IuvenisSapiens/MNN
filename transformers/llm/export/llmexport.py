import os
import json
import glob
import base64
import warnings
import argparse

warnings.filterwarnings("ignore")
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'

import onnx
import torch
from typing import Optional, List
from transformers import AutoConfig, AutoModel, AutoModelForCausalLM, AutoTokenizer

from utils.spinner import spinner_run
from utils.custom_op import FakeLinear
from utils.onnx_rebuilder import OnnxRebuilder
from utils.mnn_converter import MNNConveter
from utils.awq_quantizer import AwqQuantizer
from utils.model_mapper import ModelMapper
from utils.transformers import Embedding, Rotary, Decoder, Lm

class LlmExporter(torch.nn.Module):
    '''
    Base class for all llm model export. Inherits from [`torch.nn.Module`].
    '''
    def __init__(self, args):
        super().__init__()
        self.init_from_args(args)
        self.load_model(args.path)

    def init_from_args(self, args):
        self.visual = None
        self.audio = None
        self.talker = None
        self.mtp = None
        self.args = args
        self.max_length = 1024
        self.stop_ids = []
        self.dst_name = 'llm'
        # load config from args
        self.onnx_path = os.path.join(self.args.dst_path, 'onnx')
        if self.args.tokenizer_path is None:
            self.args.tokenizer_path = self.args.path
        if args.lm_quant_bit is None:
            self.args.lm_quant_bit = self.args.quant_bit
        # init export dst dir
        if not os.path.exists(self.args.dst_path):
            os.makedirs(self.args.dst_path)
        if not os.path.exists(self.onnx_path):
            os.makedirs(self.onnx_path)

    def load_pretrained(self, model_path: str):
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(self.args.tokenizer_path, trust_remote_code=True, use_fast=False)
        except:
            self.tokenizer = None
        if None == self.tokenizer:
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(self.args.tokenizer_path, trust_remote_code=True)
            except:
                self.tokenizer = None
        if None == self.tokenizer:
            print("Default load tokenizer failed for ", model_path)
        if 'Qwen2.5-Omni' in model_path:
            from transformers import Qwen2_5OmniForConditionalGeneration
            self.model = Qwen2_5OmniForConditionalGeneration.from_pretrained(model_path, torch_dtype="auto").eval()
        elif 'Qwen2.5-VL' in model_path or 'Qwen2___5-VL' in model_path:
            from transformers import Qwen2_5_VLForConditionalGeneration
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'Qwen2-VL' in model_path:
            from transformers import Qwen2VLForConditionalGeneration
            self.model = Qwen2VLForConditionalGeneration.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'deepseek-vl' in model_path:
            from deepseek_vl.models import VLChatProcessor, MultiModalityCausalLM
            vl_chat_processor = VLChatProcessor.from_pretrained(model_path)
            self.tokenizer = vl_chat_processor.tokenizer
            self.processor = vl_chat_processor
            vl_gpt: MultiModalityCausalLM = AutoModelForCausalLM.from_pretrained(model_path, trust_remote_code=True).eval()
            self.model = vl_gpt
            self.model.config.model_type = "deepseek-vl"
        elif 'Qwen2-Audio' in model_path:
            from transformers import Qwen2AudioForConditionalGeneration
            self.audio = Qwen2AudioForConditionalGeneration.from_pretrained(model_path, torch_dtype="auto")
            self.model = self.audio.language_model
        elif 'Llama-3.2' in model_path and 'Vision' in model_path:
            from transformers import MllamaForConditionalGeneration
            self.model = MllamaForConditionalGeneration.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'Llama' in model_path or 'Yi' in model_path:
            from transformers import LlamaForCausalLM
            self.model = LlamaForCausalLM.from_pretrained(model_path, torch_dtype='auto', trust_remote_code=True).eval()
        elif 'InternVL' in model_path:
            self.model = AutoModel.from_pretrained(model_path, torch_dtype=torch.float32, trust_remote_code=True).eval()
        elif 'gemma-3-4b-it' in model_path:
            from transformers import Gemma3ForConditionalGeneration
            self.model = Gemma3ForConditionalGeneration.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'gemma-3-1b-it' in model_path:
            from transformers import Gemma3ForCausalLM
            self.model = Gemma3ForCausalLM.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'SmolVLM2' in model_path:
            from transformers import AutoModelForImageTextToText
            self.model = AutoModelForImageTextToText.from_pretrained(model_path, torch_dtype='auto').eval()
        elif 'SmolVLM' in model_path or 'SmolDocling' in model_path:
            from transformers import AutoModelForVision2Seq
            self.model = AutoModelForVision2Seq.from_pretrained(model_path, torch_dtype='auto').eval()
        else:
            try:
                self.model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype='auto', trust_remote_code=True).eval()
            except:
                self.model = AutoModel.from_pretrained(model_path, torch_dtype='auto', trust_remote_code=True).eval()
        self.config = self.model.config
        if self.args.lora_path is not None and not self.args.lora_split:
            from peft import PeftModel
            adapter = PeftModel.from_pretrained(self.model, model_id=self.args.lora_path)
            self.model = adapter.merge_and_unload(progressbar=True)

    @staticmethod
    def has_attr(obj, attr):
        return hasattr(obj, attr) and getattr(obj, attr) is not None

    @spinner_run(f'load pretrained model ', True)
    def load_model(self, model_path):
        self.load_pretrained(model_path)
        self.attention_mask_type = 'float'
        # load tokenizer info
        self.stop_ids.append(self.tokenizer.eos_token_id)
        if hasattr(self.tokenizer, 'im_end_id'):
            self.stop_ids.append(self.tokenizer.im_end_id)
        try:
            eot_id = self.tokenizer.encode('<|eot_id|>')
            if len(eot_id) == 1:
                self.stop_ids.append(eot_id[0])
            # gemma/gemma-2
            eot_id = self.tokenizer.encode('<end_of_turn>')
            if len(eot_id) == 2 and eot_id[0] == 2:
                self.stop_ids.append(eot_id[1])
        except:
            pass
        if hasattr(self.model, 'generation_config') and self.model.generation_config is not None:
            eos_token_id = self.model.generation_config.eos_token_id
            from collections.abc import Iterable
            if isinstance(eos_token_id, int):
                self.stop_ids.append(eos_token_id)
            elif isinstance(eos_token_id, Iterable):
                for id in eos_token_id:
                    self.stop_ids.append(id)
        self.stop_ids = [stop_id for stop_id in self.stop_ids if stop_id is not None]
        self.stop_ids = list(set(self.stop_ids))
        model_mapper = ModelMapper()
        self.model_type, self.model_map = model_mapper.get_map(self.config)

        if self.args.awq:
            self.model.float()
        if self.args.export is not None:
            # set norm's weight as float for export
            def visit_module(module):
                if not isinstance(module, torch.nn.Linear) and hasattr(module, 'weight'):
                    module.float()
                for name, child in module.named_children():
                    visit_module(child)
            visit_module(self.model)
        # print(self.config, self.model_type, self.model_map, self.model)
        # load config info
        ModelMapper.do_map(self, self.config, self.model_map['config'])
        if not hasattr(self, 'num_key_value_heads') or self.num_key_value_heads is None:
            self.num_key_value_heads = self.num_attention_heads
        if not hasattr(self, 'rope_theta') or self.rope_theta is None:
            self.rope_theta = 10000.0
        if not hasattr(self, 'rope_ratio') or self.rope_ratio is None:
            self.rope_ratio = 1.0
        if not hasattr(self, 'head_dim') or self.head_dim is None:
            if isinstance(self.num_attention_heads, list):
                self.head_dim = [self.hidden_size // atten_head for atten_head in self.num_attention_heads]
            else:
                self.head_dim = self.hidden_size // self.num_attention_heads
        # some export info
        if isinstance(self.num_attention_heads, list):
            self.past_kv_shape = [self.num_hidden_layers, 2, 1, 0, self.num_key_value_heads[0], self.head_dim]
        else:
            self.past_kv_shape = [self.num_hidden_layers, 2, 1, 0, self.num_key_value_heads, self.head_dim]
        self.block_dynamic_axes = {
            "inputs_embeds" : { 0: "seq_len" },
            "attention_mask" : { 2: "seq_len", 3: "seq_len" },
            "position_ids" : { 0: "seq_len" },
            "past_key_values" : { 1: "history_len" }
        }
        self.model_dynamic_axes = {
            "input_ids" : { 0: "seq_len" },
            "attention_mask" : { 2: "seq_len", 3: "seq_len" },
            "position_ids" : { 1: "seq_len" },
            "past_key_values" : { 3: "history_len" }
        }
        prompt_template = self.build_prompt_template()
        self.llm_config = {
            'hidden_size' : self.hidden_size,
            'layer_nums' : self.num_hidden_layers,
            'attention_mask': self.attention_mask_type,
            'key_value_shape': self.past_kv_shape[1:],
            "bos": prompt_template['bos'],
            "system_prompt_template": prompt_template['system'].format(content='%s'),
            'user_prompt_template': prompt_template['user'].format(content='%s'),
            'assistant_prompt_template': prompt_template['assistant'].format(content='%s'),
            'is_visual': False
        }
        if 'jinja' in prompt_template:
            self.llm_config['jinja'] = prompt_template['jinja']
        # load modules
        ModelMapper.do_map(self, self.model, self.model_map['model'])
        # rebuild modules
        if self.lm_ is None:
            out_features, in_features = self.embed_.weight.shape
            self.lm_ = torch.nn.Linear(in_features, out_features)
            self.lm_.weight = self.embed_.weight
        elif not isinstance(self.lm_, torch.nn.Linear):
            # for Baichuan2
            weight = self.lm_.weight
            out_features, in_features = weight.shape
            self.lm_ = torch.nn.Linear(in_features, out_features)
            self.lm_.weight = weight
            self.lm_.bias.data = torch.zeros(out_features, dtype=weight.dtype)

        if self.embed_.weight is self.lm_.weight:
            import copy
            embed_copy = copy.deepcopy(self.embed_)
            self.embed = Embedding(embed_copy, self)
        else:
            self.embed = Embedding(self.embed_, self)
        # tie word embeddings
        self.tie_word_embeddings = not self.args.seperate_embed and self.lm_.weight.equal(self.embed_.weight)
        if self.tie_word_embeddings:
            print("Tie word embeddings in lm, set lm quant bit to 8")
            self.args.lm_quant_bit = 8
        # Rotary
        self.rotary = Rotary(self)
        self.blocks = []
        for block in self.blocks_.children():
            layer_id = len(self.blocks)
            self.blocks.append(Decoder(block, layer_id, self))
        self.lm = Lm(self.lm_)

        # visual model
        if self.visual is not None:
            if self.args.export is not None:
                self.visual.float()
            from utils.vision import Vision
            self.visual = Vision.get_vision(self.model_type)(self.visual, self)
        if hasattr(self, 'audio') and self.audio is not None:
            from utils.audio import Audio
            self.audio = Audio.get_audio(self.audio.config.model_type)(self.audio, self)
        else:
            self.audio = None
        # talker model
        if hasattr(self, 'talker') and self.talker is not None and \
           hasattr(self, 'token2wav') and self.token2wav is not None:
            from utils.talker import Talker
            self.talker = Talker.get_talker(self.model_type)(self.talker, self.token2wav, self)
        # MTP model
        if self.model_type == 'poi_qwen2_mtp':
            self.mtp = [self.mtp1, self.mtp2]
        if self.mtp is not None:
            if self.args.export is not None:
                for mtp_model in self.mtp:
                    mtp_model.float()
            from utils.mtp import Mtp
            self.mtp = Mtp.get_mtp(self.model_type)(self.mtp, self)
        return model_path

    def get_attention_mask(self) -> torch.Tensor:
        if self.model_type == 'chatglm':
            return self.chatglm_attention_mask()
        if self.token_len:
            return torch.zeros([1, 1, 1, self.seq_len], dtype=torch.float32)
        return (1 - torch.tril(torch.ones([1, 1, self.seq_len, self.seq_len]))) * torch.finfo(torch.float32).min

    def get_position_ids(self, input_ids = None) -> torch.Tensor:
        if self.visual is not None and hasattr(self.visual, 'get_position_ids') and callable(getattr(self.visual, 'get_position_ids')):
            return self.visual.get_position_ids(input_ids, self.seq_len, self.token_len)
        if self.model_type == 'chatglm':
            return self.chatglm_position_ids()
        if self.token_len:
            return torch.tensor([[self.seq_len - 1]], dtype=torch.int)
        return torch.arange(self.seq_len, dtype=torch.int).unsqueeze(0)

    def chatglm_attention_mask(self):
        if self.token_len:
            return torch.zeros([1]).bool().reshape([1, 1, 1, 1])
        attention_mask = torch.zeros([self.seq_len, self.seq_len], dtype=torch.bool)
        for i in range(self.seq_len - 1):
            attention_mask[i][-1] = True
        attention_mask = attention_mask.reshape([1, 1, self.seq_len, self.seq_len])
        return attention_mask

    def chatglm_position_ids(self):
        if self.token_len:
            return torch.tensor([self.context_len, self.token_len + 1]).reshape([1, 2, 1])
        position_ids_0 = torch.arange(self.seq_len, dtype=torch.int)
        position_ids_1 = torch.zeros(self.seq_len, dtype=torch.int)
        position_ids_0[-1] = position_ids_0[-2]
        position_ids_1[-1] = 1
        position_ids = torch.stack([position_ids_0, position_ids_1]).view(1, 2, -1)
        return position_ids

    def visual_embed(self, input_ids):
        return self.visual.embed(input_ids)

    def audio_embed(self, input_ids):
        return self.audio.embed(input_ids)

    def embedding(self, input_ids):
        if self.visual is not None and self.token_len == 0:
            input_embeds = self.visual_embed(input_ids)
        elif self.audio is not None and self.token_len == 0:
            input_embeds = self.audio_embed(input_ids)
        else:
            input_embeds = self.embed(input_ids)
        return input_embeds

    def forward(self,
                input_ids: torch.Tensor,
                attention_mask: torch.Tensor,
                position_ids: torch.Tensor,
                past_key_values: Optional[List[torch.Tensor]] = None,
                logits_index: int = -1,
                cross_attention_states: Optional[torch.Tensor] = None,
                cross_attention_mask: Optional[torch.Tensor] = None,
                ):
        hidden_states = input_ids # llm forward without embedding
        if self.model_type == 'gemma':
            normalizer = torch.tensor(self.hidden_size**0.5, dtype=hidden_states.dtype)
            hidden_states = hidden_states * normalizer
        if self.model_type == 'gemma3' or self.model_type == 'gemma3_text': # if --test, comments these
            hidden_states = hidden_states * self.embed.embed_scale
        presents = [None for i in range(len(self.blocks))]
        rotary_pos_emb = self.rotary(position_ids)
        if self.args.test and rotary_pos_emb.dtype != hidden_states.dtype:
            rotary_pos_emb = rotary_pos_emb.type(hidden_states.dtype)
        for i in range(len(self.blocks)):
            if self.blocks[i].cross_decoder and cross_attention_states is None:
                continue
            if past_key_values is not None and past_key_values[i] is not None:
                past_kv = past_key_values[i]
            else:
                past_kv = None
            hidden_states, kv = self.blocks[i](hidden_states, rotary_pos_emb, attention_mask, past_kv)
            presents[i] = kv
        talker_embeds = None
        if hasattr(self, 'talker') and self.talker is not None:
            talker_embeds = self.final_layernorm_(hidden_states) + input_ids.permute([1, 0, 2])
            self.talker.add_talker_embeds(talker_embeds)

        final_layernorm = hidden_states
        if self.mtp is None:
            hidden_states = hidden_states[:, logits_index:, :]
            hidden_states = self.final_layernorm_(hidden_states)
            # default: set hidden_state before lm_head as output node
            final_layernorm = hidden_states
        else:
            # final_layernorm need compute all logists
            if self.model_type == 'mimo':
                final_layernorm = hidden_states # mimo
            hidden_states = self.final_layernorm_(hidden_states)
            if self.model_type == 'poi_qwen2_mtp':
                final_layernorm = hidden_states # poi
            hidden_states = hidden_states[:, logits_index:, :]
        logits = self.lm(hidden_states)
        if presents[0].shape == presents[-1].shape and None not in presents:
            presents = torch.stack(presents)
        self.seq_len += 1
        self.token_len += 1
        return logits, final_layernorm, presents, talker_embeds

    # some test functions
    def build_prompt_template(self):
        template = {
            'bos': '',
            'system': '{content}',
            'user': '{content}',
            'assistant': '{content}'
        }
        if hasattr(self.tokenizer, 'get_chat_template'):
            template['jinja'] = {}
            template['jinja']['chat_template'] = self.tokenizer.get_chat_template()
            if None != self.tokenizer.bos_token:
                template['jinja']['bos'] = self.tokenizer.bos_token
            if None != self.tokenizer.eos_token:
                template['jinja']['eos'] = self.tokenizer.eos_token
        if self.model_type == 'baichuan':
            template['user'] = '<reserved_106>{content}'
            template['assistant'] = '<reserved_107>{content}'
        if self.model_type == 'chatglm':
            template['user'] = '{content}[gMASK]<sop>'
        if self.model_type == 'chatglm2' and 'codegeex' not in self.args.path:
            template['user'] = '[Round 1]\n\n问：{content}\n\n'
            template['assistant'] = '答：{content}\n\n'
            if 'chatglm3' in self.args.path or 'glm-4' in self.args.path:
                template['bos'] = '[gMASK]<sop>'
                template['system'] = '<|system|>\n{content}\n'
                template['user'] = '<|user|>\n{content}\n'
                template['assistant'] = '<|assistant|>\n{content}\n'
        if self.model_type == 'llama':
            if 'Llama-2' in self.args.path:
                template['bos'] = '[INST] '
                template['system'] =  "<<SYS>>\n{content}\n<</SYS>>\n\n"
                template['user'] = '{content} [/INST]'
                template['assistant'] = "{content}</s>";
            if 'Llama-3' in self.args.path:
                template['system'] = '<|start_header_id|>system<|end_header_id|>\n\n{content}<|eot_id|>'
                template['user'] = '<|start_header_id|>user<|end_header_id|>\n\n{content}<|eot_id|>'
                template['assistant'] = '<|start_header_id|>assistant<|end_header_id|>\n\n{content}<|eot_id|>'
            if 'TinyLlama' in self.args.path:
                template['bos'] = '<s>'
                template['system'] = '<|system|>\n{content}</s>\n'
                template['user'] = '<|user|>\n{content}</s>\n'
                template['assistant'] = '<|assistant|>\n{content}</s>\n'
            if 'Yi' in self.args.path or 'SmolLM2' in self.args.path:
                template['system'] = '<|im_start|>system\n{content}<|im_end|>\n'
                template['user'] = '<|im_start|>user\n{content}<|im_end|>\n'
                template['assistant'] = '<|im_start|>assistant\n{content}<|im_end|>\n'
        if self.model_type == 'gemma2':
            template['bos'] = '<bos>'
            template['system'] = '<start_of_turn>system\n{content}<end_of_turn>\n'
            template['user'] = '<start_of_turn>user\n{content}<end_of_turn>\n'
            template['assistant'] = '<start_of_turn>model\n{content}<end_of_turn>\n'
        if self.model_type == 'gemma':
            template['bos'] = '<bos>'
        if self.model_type == 'internlm':
            template['user'] = '<|User|>:{content}<eoh>\n'
            template['assistant'] = '<|Bot|>:{content}<eoh>\n'
        if self.model_type == 'phi-msft':
            template['user'] = 'Instruct: {content}\n'
            template['assistant'] = 'Output:{content}\n'
        if self.model_type == 'openelm':
            template['bos'] = '<s>'
        if self.model_type == 'internvl_chat':
            if 'Qwen' in self.config.llm_config._name_or_path:
                print("[DEBUG] Use qwen prompt template")
                template['system'] = '<|im_start|>system\n{content}<|im_end|>\n'
                template['user'] = '<|im_start|>user\n{content}<|im_end|>\n'
                template['assistant'] = '<|im_start|>assistant\n{content}<|im_end|>\n'
        if self.model_type == 'phi3':
            template['system'] = '<|im_start|>system<|im_sep|>{content}<|im_end|>'
            template['user'] = '<|im_start|>user<|im_sep|>{content}<|im_end|>'
            template['assistant'] = '<|im_start|>assistant<|im_sep|>{content}<|im_end|>'

        if self.model_type == "gemma3":
            template['bos'] = '<bos><start_of_turn>user\n'
            template['system'] = '{content}\n\n'
            template['user'] = '{content}<end_of_turn>\n'
            template['assistant'] = '<start_of_turn>model\n{content}<end_of_turn>\n<start_of_turn>user\n'
        if self.model_type == "gemma3_text":
            template['bos'] = '<bos><start_of_turn>user\n'
            template['system'] = '{content}\n\n'
            template['user'] = '{content}<end_of_turn>\n'
            template['assistant'] = '<start_of_turn>model\n{content}<end_of_turn>\n<start_of_turn>user\n'
        if self.model_type in ['idefics3', 'smolvlm']:
            template['bos'] = '<|im_start|>'
            template['system'] = 'System: {content}<end_of_utterance>\n'
            template['user'] = 'User:{content}<end_of_utterance>\n'
            template['assistant'] = 'Assistant:{content}<end_of_utterance>\n'
        if 'qwen' in self.model_type or 'mimo' in self.model_type:
            template['system'] = '<|im_start|>system\n{content}<|im_end|>\n'
            template['user'] = '<|im_start|>user\n{content}<|im_end|>\n'
            template['assistant'] = '<|im_start|>assistant\n{content}<|im_end|>\n'
        if 'DeepSeek' in self.args.path or 'deepseek' in self.args.path:
            template['bos'] = '<|begin_of_sentence|>'
            template['system'] = '{content}\n'
            template['user'] = '\nUser: {content}\n'
            template['assistant'] = '\nAssistant: {content}\n<|end_of_sentence|>'
        if self.model_type == "ernie4_5":
            template['bos'] = '<|begin_of_sentence|>'
            template['user'] = 'User: {content}\n'
            template['assistant'] = 'Assistant: {content}\n<|end_of_sentence|>'
        return template

    def build_prompt(self, messages):
        template = self.build_prompt_template()
        prompt = template['bos']
        for item in messages:
            role, content = item['role'], item['content']
            if '{content}' in template[role]:
                prompt += template[role].format(content=content)
            else:
                prompt += role + '\n' + content +'\n'
        assistant_prefix = template['assistant'].split('{content}')[0]
        return prompt + assistant_prefix

    def str_to_ids(self, prompt):
        if self.visual is not None:
            return self.visual.str_to_ids(prompt)
        if self.audio is not None:
            return self.audio.str_to_ids(prompt)
        input_ids = self.tokenizer(prompt, add_special_tokens=False, return_tensors="pt")['input_ids']
        return input_ids

    def id_to_str(self, token_id):
        try:
            word = self.tokenizer.decode(int(token_id))
        except:
            def contains_replacement(text): return '\uFFFD' in text
            def decode_id(token_id):
                return self.tokenizer.convert_tokens_to_string(
                        self.tokenizer._convert_id_to_token(int(token_id)))
            def decode_ids(token_ids):
                return self.tokenizer.convert_tokens_to_string(
                        self.tokenizer.convert_ids_to_tokens(token_ids))
            word = decode_id(int(token_id))
            # Smollm tokenizer will produce half chinese character, using buffer to decode
            if contains_replacement(word):
                self.decode_buffer.append(token_id)
                buffer_txt = decode_ids(self.decode_buffer)
                if not contains_replacement(buffer_txt):
                    word = buffer_txt
                    self.decode_buffer.clear()
                else:
                    word = ''
        return word

    @torch.no_grad()
    def response(self, query):
        # self.imitate_quant()
        self.decode_buffer = []
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": query}
        ]
        prompt = self.build_prompt(messages)
        input_ids = self.str_to_ids(prompt)
        if self.visual is not None:
            cross_attention_states = self.visual.cross_attention_states
            cross_attention_mask = self.visual.cross_attention_mask
        else:
            cross_attention_states = None
            cross_attention_mask = None
        self.seq_len = input_ids.numel()
        self.context_len = self.seq_len - 2
        self.token_len = 0
        past_key_values = [None for i in range(self.num_hidden_layers)]
        token_id = input_ids
        while self.token_len < self.max_length:
            attention_mask = self.get_attention_mask()
            position_ids = self.get_position_ids(token_id)
            input_ids = self.embedding(token_id)
            logits, _, past_key_values, _ = self.forward(input_ids,
                                                      attention_mask,
                                                      position_ids,
                                                      past_key_values,
                                                      cross_attention_states = cross_attention_states,
                                                      cross_attention_mask = cross_attention_mask)
            token_id = torch.argmax(logits[:,-1,:])
            if token_id in self.stop_ids:
                print("", end='\n')
                break
            word = self.id_to_str(token_id)
            print(word, end="", flush=True)

        if hasattr(self, 'talker') and self.talker is not None:
            self.talker.generate()

    def export_mtp(self):
        if self.mtp is None:
            return
        mtp_onnx = self.mtp.export(self.onnx_path)
        if self.mnn_converter:
            self.mtp.unloaded_ops['/lm/lm_head/Linear'] = self.unloaded_ops['/lm/lm_head/Linear']
            MNNConveter(self, self.mtp.unloaded_ops).export(mtp_onnx)

    @spinner_run(f'export embedding to ')
    def export_embed(self):
        import ctypes
        if hasattr(self, 'word_embeddings'):
            # embedding model's embed
            tensor_data = self.word_embeddings.weight.data.bfloat16()
        else:
            tensor_data = self.embed.embed.weight.data.bfloat16()
        data_ptr = tensor_data.untyped_storage().data_ptr()
        buffer = (ctypes.c_byte * (tensor_data.numel() * 2)).from_address(data_ptr)
        embedding_file = f'{self.args.dst_path}/embeddings_bf16.bin'
        with open(embedding_file, 'wb') as f:
            f.write(buffer)
        return embedding_file

    @spinner_run(f'export config to ')
    def export_config(self, mnn_config = False):
        config_json = f'{self.args.dst_path}/llm_config.json'
        with open(config_json, 'w', encoding='utf-8') as f:
            json.dump(self.llm_config, f, ensure_ascii=False, indent=4)
        if not mnn_config:
            return config_json
        with open(f'{self.args.dst_path}/config.json', 'w', encoding='utf-8') as f:
            config = {
                "llm_model": f"{self.dst_name}.mnn",
                "llm_weight": f"{self.dst_name}.mnn.weight",
                "backend_type": "cpu",
                "thread_num": 4,
                "precision": "low",
                "memory": "low",
                # "system_prompt": "You are a helpful assistant.",
                "sampler_type":'penalty',
                "penalty":1.1
            }
            if self.talker is not None:
                config['system_prompt'] = "You are Qwen, a virtual human developed by the Qwen Team, Alibaba Group, capable of perceiving auditory and visual inputs, as well as generating text and speech."
                config['talker_max_new_tokens'] = 2048
                config['talker_speaker'] = "Chelsie"
                config['dit_steps'] = 5
                config['dit_solver'] = 1
            if self.model_type == "gemma3":
                config.update({'precision': "normal"})
            if self.visual is not None or self.audio is not None:
                config['mllm'] = {
                    'backend_type': "cpu",
                    "thread_num": 4,
                    "precision": "normal",
                    "memory": "low"
                }
            json.dump(config, f, ensure_ascii=False, indent=4)
        return config_json

    def imitate_quant(self):
        def quant_dequant(linear, quant_bit = self.args.quant_bit, quant_block = self.args.quant_block):
            weight = linear.weight.data
            oc, ic = weight.shape
            if quant_block == 0:
                block_size = ic
            else:
                block_size = quant_block
            block_num = ic // block_size
            weight = weight.reshape(oc, block_num, block_size)
            max_val = torch.max(weight, axis=-1, keepdims=True).values
            min_val = torch.min(weight, axis=-1, keepdims=True).values
            offset = 1 << (quant_bit - 1)
            clip_max = offset - 1
            clip_min = -offset
            scale = (max_val - min_val) / (clip_max - clip_min)
            q_weight = torch.round((weight - min_val) / scale) + clip_min
            q_weight = torch.clip(q_weight, clip_min, clip_max)
            dq_weight = (q_weight - clip_min) * scale + min_val
            dq_weight = dq_weight.reshape(oc, ic).float()
            linear.weight.data = dq_weight
            return linear
        with torch.no_grad():
            for i in range(self.num_hidden_layers):
                for name, child in self.blocks[i].self_attn.named_children():
                    if isinstance(child, torch.nn.Linear):
                        setattr(self.blocks[i].self_attn, name, quant_dequant(child))
                for name, child in self.blocks[i].mlp.named_children():
                    if isinstance(child, torch.nn.Linear):
                        setattr(self.blocks[i].mlp, name, quant_dequant(child))
            self.lm.lm = quant_dequant(self.lm.lm)

    def unload_param(self):
        self.unloaded_ops = {}
        self.experts = []
        def build_faker(real, name):
            faker = FakeLinear(real.in_features, real.out_features, real.bias is not None, name)
            self.unloaded_ops[name] = real
            return faker
        # replace linear with fakelinear to save export memory and time
        with torch.no_grad():
            for i in range(len(self.blocks)):
                # different kv cache shape in different layers
                if isinstance(self.num_attention_heads, list):
                    self.blocks[i].self_attn.export_fused_attn = True
                is_moe = hasattr(self.blocks[i].mlp, 'is_moe') and self.blocks[i].mlp.is_moe
                if is_moe:
                    self.blocks[i].mlp.export_moe = True
                for name, child in self.blocks[i].self_attn.named_children():
                    if isinstance(child, torch.nn.Linear):
                        setattr(self.blocks[i].self_attn, name, build_faker(child, f'/layers.{i}/self_attn/{name}/Linear'))
                for name, child in self.blocks[i].mlp.named_children():
                    if isinstance(child, torch.nn.Linear):
                        setattr(self.blocks[i].mlp, name, build_faker(child, f'/layers.{i}/mlp/{name}/Linear'))
                    if is_moe and isinstance(child, torch.nn.ModuleList): # experts
                        self.experts.append(child)
                        for j in range(len(child)):
                            for name, cchild in child[j].named_children():
                                if isinstance(cchild, torch.nn.Linear):
                                    setattr(self.blocks[i].mlp.experts[j], name, build_faker(cchild, f'/expert/{i}_{j}/{name}'))
            self.lm.lm = build_faker(self.lm.lm, f'/lm/lm_head/Linear')

    @spinner_run(f'export model weight to ')
    def onnx_load_param(self, onnx_path):
        return OnnxRebuilder(onnx_path, self.unloaded_ops).rebuild()

    @spinner_run(f'slim the graph of ')
    def slim_onnx(self, onnx_model):
        import onnxslim
        model = onnxslim.slim(onnx_model)
        onnx.save(model, onnx_model)
        return onnx_model

    @spinner_run(f'export onnx model to ')
    def export_onnx(self):
        # unload linear weight to save export memory
        self.unload_param()
        model = self
        self.seq_len = 3
        self.token_len = 0
        input_ids = torch.arange(3, dtype=torch.long)
        attention_mask =  self.get_attention_mask()
        position_ids = self.get_position_ids(input_ids)
        onnx_model = f'{self.onnx_path}/{self.dst_name}.onnx'
        # For export onnx, don't need image or audio's embedding
        input_ids = self.embed(input_ids)
        past_key_values = torch.zeros(self.past_kv_shape)
        logits_index = torch.tensor([-1], dtype=torch.int32)
        if hasattr(self, 'talker') and self.talker is not None:
            output_names = ['logits', 'hidden_states', 'presents', 'talker_embeds']
        else:
            output_names = ['logits', 'hidden_states', 'presents']
        # export to onnx
        torch.onnx.export(
            model, (input_ids, attention_mask, position_ids, past_key_values, logits_index),
            onnx_model,
            input_names=[
                'input_ids', 'attention_mask', 'position_ids', 'past_key_values', 'logits_index'
            ],
            output_names=output_names,

            dynamic_axes=self.model_dynamic_axes,
            do_constant_folding=True,
            verbose=False,
            opset_version=15)
        return onnx_model

    def awq_quant(self):
        self.awq_quantizer = AwqQuantizer(self)
        self.awq_quantizer.quantize()
        self.is_awq_quantized = True

    def export_vision(self):
        if self.visual is None:
            return
        vision_onnx = self.visual.export(self.onnx_path)
        if self.mnn_converter:
            fuse_transformer = self.visual.transformer_fuse
            native_group_conv = self.visual.group_conv_native
            if self.args.transformer_fuse:
                fuse_transformer = True
            if self.args.group_conv_native:
                native_group_conv = True
            self.mnn_converter.export(vision_onnx, self.visual.quant_bit,
                                      self.visual.quant_block,
                                      transformer_fuse=fuse_transformer,
                                      group_conv_native=native_group_conv)

    def export_audio(self):
        if self.audio is None:
            return
        audio_onnx = self.audio.export(self.onnx_path)
        if self.mnn_converter: self.mnn_converter.export(audio_onnx, self.audio.quant_bit)

    def export_talker(self):
        if self.talker is None:
            return
        talker_onnx = self.talker.export(self.onnx_path)
        predit_onnx, dit_onnx, bigvgan_onnx = self.talker.token2wav.export(self.onnx_path)
        if self.mnn_converter:
            self.mnn_converter.export(talker_onnx, self.talker.quant_bit)
            self.mnn_converter.export(predit_onnx, self.talker.token2wav.quant_bit)
            self.mnn_converter.export(dit_onnx, self.talker.token2wav.quant_bit)
            self.mnn_converter.export(bigvgan_onnx, self.talker.token2wav.quant_bit)

    def export_language(self):
        # export_embedding
        if self.mnn_converter and self.tie_word_embeddings:
            pass # mnn tie_word_embeddings need't export embedding
        else:
            self.export_embed()
        # export transformer
        onnx_model = self.export_onnx()

        if self.args.onnx_slim:
            self.slim_onnx(onnx_model)
        if self.mnn_converter:
            MNNConveter(self, self.unloaded_ops).export(onnx_model)
        else:
            self.onnx_load_param(onnx_model)

    def export(self, export_type):
        if self.args.awq:
            self.awq_quant()
        if self.args.hqq and self.args.sym:
            self.args.sym = False
        export_mnn = export_type == 'mnn'
        self.mnn_converter = MNNConveter(self) if export_mnn else None
        self.export_talker()
        self.export_vision()
        self.export_audio()
        self.export_language()
        self.export_mtp()
        self.export_tokenizer()
        self.export_config(export_mnn)
        if export_mnn:
            # delete onnx file
            try:
                for file in glob.glob(f'{self.onnx_path}/*'):
                    os.remove(file)
                os.rmdir(self.onnx_path)
            except Exception as e:
                print(f"remove onnx error: {e}")

    @spinner_run(f'export tokenizer to ')
    def export_tokenizer(self):
        # load tokenizer file
        tokenizer_model = os.path.join(self.args.tokenizer_path, 'tokenizer.model')
        ice_text_model = os.path.join(self.args.tokenizer_path, 'ice_text.model')
        try:
            import sentencepiece as spm
            if os.path.exists(tokenizer_model):
                self.sp_model = spm.SentencePieceProcessor(tokenizer_model)
            elif os.path.exists(ice_text_model):
                self.sp_model = spm.SentencePieceProcessor(ice_text_model)
            else:
                self.sp_model = None
        except:
            self.sp_model = None
        merge_file = os.path.join(self.args.path, 'merges.txt')
        if os.path.exists(merge_file):
            self.merge_txt = merge_file
        else:
            self.merge_txt = None
        # TOKENIZER MAGIC NUMBER
        MAGIC_NUMBER = 430
        # TOKENIZER TYPE
        SENTENCEPIECE = 0; TIKTOIKEN = 1; BERT = 2; HUGGINGFACE = 3
        def write_line(fp, *args):
            for arg in args:
                for token in arg:
                    fp.write(str(token) + ' ')
            fp.write('\n')
        def write_header(fp, type, speicals, prefix = []):
            fp.write(f'{MAGIC_NUMBER} {type}\n')
            fp.write(f'{len(speicals)} {len(self.stop_ids)} {len(prefix)}\n')
            write_line(fp, speicals, self.stop_ids, prefix)

        file_path = os.path.join(self.args.dst_path, "tokenizer.txt")
        special_list = list(self.tokenizer.added_tokens_decoder.keys())
        if hasattr(self.tokenizer, 'special_tokens'):
            for k, v in self.tokenizer.special_tokens.items():
                special_list.append(v)
        if hasattr(self.tokenizer, 'all_special_ids'): #gemma3
            special_list.extend(self.tokenizer.all_special_ids)
        if hasattr(self.tokenizer, 'gmask_token_id'):
            special_list.append(self.tokenizer.gmask_token_id)
        if hasattr(self.model, 'generation_config') and self.model.generation_config is not None:
            generation_config = self.model.generation_config
            if hasattr(generation_config, 'user_token_id'):
                special_list.append(generation_config.user_token_id)
            if hasattr(generation_config, 'assistant_token_id'):
                special_list.append(generation_config.assistant_token_id)
        vocab_list = []
        prefix_list = []
        if hasattr(self.tokenizer, 'get_prefix_tokens'):
            prefix_list = self.tokenizer.get_prefix_tokens()
        if len(prefix_list) == 0:
            try:
                test_txt = 'A'
                ids = self.tokenizer.encode(test_txt)
                get_txt = self.tokenizer.decode(ids[-1])
                if len(ids) > 1 and get_txt == test_txt:
                    prefix_list += ids[:-1]
            except:
                pass

        if self.sp_model is not None:
            # senetencepiece
            NORMAL = 1; UNKNOWN = 2; CONTROL = 3
            USER_DEFINED = 4; UNUSED = 5; BYTE = 6
            for i in range(self.sp_model.GetPieceSize()):
                token = self.sp_model.IdToPiece(i)
                score = self.sp_model.GetScore(i)
                token_type = NORMAL
                if self.sp_model.IsUnknown(i):
                    token_type = UNKNOWN
                elif self.sp_model.IsControl(i):
                    token_type = CONTROL
                elif self.sp_model.IsUnused(i):
                    token_type = UNUSED
                elif self.sp_model.IsByte(i):
                    token_type = BYTE
                if self.args.path == 'Chatglm_6b':
                    if '<n>' in token: token = '\n'
                    if '<|tab|>' in token: token = '\t'
                    if '<|blank_' in token: token = ' ' * int(token[8:token.find('|>')])
                if '▁' in token: token = token.replace('▁', ' ')
                token_encode = base64.b64encode(token.encode("utf-8")).decode("utf8")
                vocab_list.append(f'{token_encode} {score} {token_type}\n')
            # add special tokens to vocab_list
            for index in special_list:
                if index >= len(vocab_list):
                    token = self.tokenizer.decode(index)
                    token_encode = base64.b64encode(token.encode("utf-8")).decode("utf8")
                    vocab_list.append(f'{token_encode} {0} {NORMAL}\n')
            with open(file_path, "w", encoding="utf8") as fp:
                write_header(fp, SENTENCEPIECE, special_list, prefix_list)
                if self.model_type == "gemma3" or self.model_type == "gemma3-text":
                    fp.write(f'{len(vocab_list) + 1}\n') # len(vocab_list)==262144, self.tokenizer([262144])=='image_soft_token' is a special token
                else:
                    fp.write(f'{len(vocab_list)}\n')
                for vocab in vocab_list:
                    fp.write(vocab)
        elif hasattr(self.tokenizer, 'mergeable_ranks'):
            # tikton
            vocab_list = []
            for k, v in self.tokenizer.mergeable_ranks.items():
                line = base64.b64encode(k).decode("utf8") + "\n"
                vocab_list.append(line)
            if hasattr(self.tokenizer, 'special_tokens'):
                for k, v in self.tokenizer.special_tokens.items():
                    line = base64.b64encode(k.encode("utf-8")).decode("utf8") + "\n"
                    vocab_list.append(line)
            if hasattr(self.tokenizer, 'added_tokens_decoder'):
                for k, v in self.tokenizer.added_tokens_decoder.items():
                    line = base64.b64encode(v.__str__().encode("utf-8")).decode("utf8") + "\n"
                    vocab_list.append(line)
            with open(file_path, "w", encoding="utf8") as fp:
                write_header(fp, TIKTOIKEN, special_list, prefix_list)
                fp.write(f'{len(vocab_list)}\n')
                for vocab in vocab_list:
                    fp.write(vocab)
        elif self.merge_txt is not None:
            # huggingface tokenizer
            merge_list = []
            vocab = self.tokenizer.get_vocab()
            special_list = list(self.tokenizer.added_tokens_decoder.keys())
            vocab_list = ['<unk>' for i in range(len(vocab))]
            # load vocab
            for k, v in vocab.items():
                vocab_list[int(v)] = k
            # load merge
            with open(self.merge_txt, 'rt') as merge:
                for line in merge.readlines():
                    merge_list.append(line)
            # write to tokenizer.txt
            with open(file_path, "w", encoding="utf8") as fp:
                write_header(fp, HUGGINGFACE, special_list)
                fp.write(f'{len(vocab_list)} {len(merge_list)}\n')
                for v in vocab_list:
                    fp.write(v + '\n')
                for m in merge_list:
                    fp.write(m)
        else:
            # tiktoken or bert
            if 'bert' in type(self.tokenizer).__name__.lower():
                tokenizer_type = BERT
            else:
                tokenizer_type = TIKTOIKEN
            # bert tokenizer
            def unicode_to_byte(u: int):
                if u >= 256 and u <= 288:
                    return u - 256
                if u >= 289 and u <= 322:
                    return u - 162
                if u == 323:
                    return 173
                if u == 65372: # |
                    return 124
                if u == 9601:  # _
                    return 95
                return u
            vocab = self.tokenizer.get_vocab()
            vocab_list = ['<unk>' for i in range(len(vocab))]
            for k, v in vocab.items():
                try:
                    vocab_list[int(v)] = bytes([unicode_to_byte(ord(c)) for c in k])
                except:
                    vocab_list[int(v)] = k.encode('utf-8')

            special_list = list(self.tokenizer.added_tokens_decoder.keys())
            with open(file_path, "w", encoding="utf8") as fp:
                write_header(fp, tokenizer_type, special_list)
                fp.write(f'{len(vocab_list)}\n')
                for v in vocab_list:
                    line = base64.b64encode(v).decode("utf8") + "\n"
                    fp.write(line)
        return file_path

class EmbeddingExporter(LlmExporter):
    def __init__(self, args):
        super().__init__(args)
        self.dst_name = 'embedding'

    def word_embed(self, input_ids):
        if hasattr(self, 'word_embeddings'):
            return self.word_embeddings(input_ids.view(1, -1))
        return self.embed(input_ids.view(1, -1))

    def bge_forward(self, inputs_embeds, attention_mask, position_ids):
        # bert absolute position
        inputs_embeds = inputs_embeds.reshape(1, -1, self.hidden_size)
        position_embeddings = self.position_embeddings(position_ids)
        embeddings = inputs_embeds + position_embeddings + self.token_type_embeddings
        hidden_states = self.embedding_layernorm(embeddings)
        for i in range(self.num_hidden_layers):
            hidden_states = self.blocks[i](hidden_states, attention_mask)[0]
        sentence_embeddings = hidden_states[:, 0]
        sentence_embeddings = torch.nn.functional.normalize(sentence_embeddings, p=2, dim=1)
        return sentence_embeddings

    def gte_forward(self, inputs_embeds, attention_mask, position_ids):
        # rope position
        inputs_embeds = inputs_embeds.reshape(1, -1, self.hidden_size)
        freqs = position_ids.float().reshape(-1, 1) * self.inv_freq
        emb = torch.cat((freqs, freqs), dim=-1)
        rope_embeds = torch.stack([emb.cos(), emb.sin()]).unsqueeze(-2).unsqueeze(1)
        attention_bias = 1 - attention_mask.float()
        hidden_states = self.embedding_layernorm(inputs_embeds + self.token_type_embeddings)
        for i in range(self.num_hidden_layers):
            hidden_states = self.blocks[i](hidden_states, attention_bias, rope_embeds)[0]
        sentence_embeddings = hidden_states[:, 0]
        sentence_embeddings = torch.nn.functional.normalize(sentence_embeddings, p=2, dim=1)
        return sentence_embeddings

    def forward(self, inputs_embeds, attention_mask, position_ids):
        if self.model_type == 'bert':
            return self.bge_forward(inputs_embeds, attention_mask, position_ids)
        if self.model_type == 'new':
            return self.gte_forward(inputs_embeds, attention_mask, position_ids)
        if self.model_type == 'qwen3':
            return super().forward(inputs_embeds, attention_mask, position_ids)
        raise RuntimeError(f'Not support embedding model: {self.model_type}!')

    def response(self, query):
        self.eval()
        input_ids = self.tokenizer(query)['input_ids']
        self.seq_len = len(input_ids)
        input_ids = torch.tensor(input_ids)
        position_ids = self.get_position_ids()
        attention_mask = self.get_attention_mask()
        inputs_embeds = self.word_embed(input_ids)
        res = self.forward(inputs_embeds, position_ids, attention_mask)
        # print(res)
        return res

    @spinner_run(f'load pretrained model ')
    def load_model(self, model_path):
        if 'Qwen3' in model_path:
            self.token_len = 0
            super().load_model(model_path)
            self.model.float()
            return model_path
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
        self.config._attn_implementation = 'eager'
        self.model = AutoModel.from_pretrained(model_path, config=self.config, trust_remote_code=True).float().eval()
        transformer = self.model.encoder
        self.model_type = self.config.model_type
        self.lm_ = self.model.pooler
        self.embed_ = self.model.embeddings
        self.word_embeddings = self.embed_.word_embeddings
        self.token_type_embeddings = self.embed_.token_type_embeddings.weight.data[0]
        self.embedding_layernorm = self.embed_.LayerNorm
        if hasattr(self.embed_, 'position_embeddings'):
            self.position_embeddings = self.embed_.position_embeddings
        self.hidden_size = self.word_embeddings.weight.shape[-1]
        self.blocks = transformer.layer
        if self.model_type == 'new':
            self.inv_freq = self.embed_.rotary_emb.inv_freq
        # some wrapper
        self.stop_ids = []
        self.num_hidden_layers = len(self.blocks)
        self.embed = self.embed_
        self.lm = self.lm_
        # some config for export
        self.model_dynamic_axes = {
            "input_ids" : { 1: "seq_len" },
            "position_ids" : { 1: "seq_len" },
            "attention_mask" : { 2: "seq_len", 3: "seq_len" }
        }
        self.attention_mask_type = 'int'
        self.llm_config = {
            'hidden_size' : self.hidden_size,
            'layer_nums' : self.num_hidden_layers,
            'attention_mask': self.attention_mask_type,
            'key_value_shape': [],
            "prompt_template": self.build_prompt('%s'),
            'is_visual': False
        }
        return model_path

    @spinner_run(f'export onnx model to ')
    def export_onnx(self):
        model = self.eval()
        self.seq_len = 3
        input_ids = torch.arange(3, dtype=torch.long)
        position_ids = self.get_position_ids()
        attention_mask = self.get_attention_mask()
        inputs_embeds = self.word_embed(input_ids)
        onnx_model = f'{self.onnx_path}/{self.dst_name}.onnx'
        torch.onnx.export(
            model, (inputs_embeds, attention_mask, position_ids),
            onnx_model,
            input_names=[
                'input_ids',
                'attention_mask',
                'position_ids'
            ],
            output_names=['sentence_embeddings'],
            dynamic_axes=self.model_dynamic_axes,
            do_constant_folding=True,
            opset_version=15)
        return onnx_model

    def export(self, export_type):
        export_mnn = 'mnn' in export_type
        self.export_tokenizer()
        self.export_config(export_mnn)
        self.export_embed()
        onnx_model = self.export_onnx()
        if self.args.onnx_slim:
            self.slim_onnx(onnx_model)
        if export_mnn:
            MNNConveter(self).export(onnx_model)
            # delete onnx file
            try:
                for file in glob.glob(f'{self.onnx_path}/*'):
                    os.remove(file)
                os.rmdir(self.onnx_path)
            except Exception as e:
                print(f"remove onnx error: {e}")

    def build_prompt(self, content):
        if self.model_type == 'bert':
            return f'[CLS]{content}[SEP]'
        if self.model_type == 'new':
            return f'<s> {content}</s>'

    def get_position_ids(self) -> torch.Tensor:
        return torch.arange(self.seq_len, dtype=torch.long).unsqueeze(0)

    def get_attention_mask(self) -> torch.Tensor:
        return torch.ones([1, 1, self.seq_len, self.seq_len], dtype=torch.float)

def export(path,
           type = None,
           tokenizer_path = None,
           lora_path = None,
           gptq_path = None,
           dst_path = './model',
           export = 'onnx',
           onnx_slim = False,
           quant_bit = 4,
           quant_block = 64,
           lm_quant_bit = None,
           mnnconvert = None,
           ppl = False,
           awq = False,
           hqq = False,
           transformer_fuse = False,
           group_conv_native = False,
           sym = False,
           seperate_embed = False,
           lora_split = False):
    args = argparse.Namespace()
    for k, v in {
        'path': path,
        'type': type,
        'tokenizer_path': tokenizer_path,
        'lora_path': lora_path,
        'gptq_path': gptq_path,
        'dst_path': dst_path,
        'export': export,
        'onnx_slim': onnx_slim,
        'quant_bit': quant_bit,
        'quant_block': quant_block,
        'lm_quant_bit': lm_quant_bit,
        'mnnconvert': mnnconvert,
        'ppl': ppl,
        'awq': awq,
        'hqq': hqq,
        'transformer_fuse': transformer_fuse,
        'group_conv_native': group_conv_native,
        'sym': sym,
        'seperate_embed': seperate_embed,
        'lora_split': lora_split
    }.items():
        setattr(args, k, v)
    if 'bge' in path:
        llm_exporter = EmbeddingExporter(args)
    else:
        llm_exporter = LlmExporter(args)
    # export
    llm_exporter.export(export)

def main():
    parser = argparse.ArgumentParser(description='llm_exporter', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--path', type=str, required=True,
                        help='path(`str` or `os.PathLike`):\nCan be either:'
                        '\n\t- A string, the *model id* of a pretrained model like `THUDM/chatglm-6b`. [TODO]'
                        '\n\t- A path to a *directory* clone from repo like `../chatglm-6b`.')
    parser.add_argument('--type', type=str, default=None,
                        help='type(`str`, *optional*):'
                        '\n\tThe pretrain llm model type.'
                        )
    parser.add_argument('--tokenizer_path', type=str, default=None, help='tokenizer path, defaut is `None` mean using `--path` value.')
    parser.add_argument('--lora_path', type=str, default=None, help='lora path, defaut is `None` mean not apply lora.')
    parser.add_argument('--gptq_path', type=str, default=None, help='gptq path, defaut is `None` mean not apply gptq.')
    parser.add_argument('--dst_path', type=str, default='./model', help='export onnx/mnn model to path, defaut is `./model`.')
    parser.add_argument('--verbose', action='store_true', help='Whether or not to print verbose.')
    parser.add_argument('--test', type=str, help='test model inference with query `TEST`.')
    parser.add_argument('--export', type=str, default=None, help='export model to an onnx/mnn model.')
    parser.add_argument('--onnx_slim', action='store_true', help='Whether or not to use onnx-slim.')
    parser.add_argument('--quant_bit', type=int, default=4, help='mnn quant bit, 4 or 8, default is 4.')
    parser.add_argument('--quant_block', type=int, default=64, help='mnn quant block, 0 mean channle-wise, default is 64.')
    parser.add_argument('--lm_quant_bit', type=int, default=None, help='mnn lm_head quant bit, 4 or 8, default is `quant_bit`.')
    parser.add_argument('--mnnconvert', type=str, default='../../../build/MNNConvert', help='local mnnconvert path, if invalid, using pymnn.')
    parser.add_argument('--ppl', action='store_true', help='Whether or not to get all logits of input tokens.')
    parser.add_argument('--awq', action='store_true', help='Whether or not to use awq quant.')
    parser.add_argument('--hqq', action='store_true', help='Whether or not to use hqq quant.')
    parser.add_argument('--transformer_fuse', action='store_true', help='Whether or not to fuse vision transformer op.')
    parser.add_argument('--group_conv_native', action='store_true', help='Whether or not to keep native group_conv.')
    parser.add_argument('--sym', action='store_true', help='Whether or not to using symmetric quant (without zeropoint), defualt is False.')
    parser.add_argument('--seperate_embed', action='store_true', help='For lm and embed shared model, whether or not to sepearte embed to avoid quant, defualt is False, if True, embed weight will be seperate to embeddingbf16.bin.')
    parser.add_argument('--lora_split', action='store_true', help='Whether or not export lora split, defualt is False.')
    args = parser.parse_args()

    model_path = args.path

    embedding_models = ['bge', 'gte', 'Qwen3-Embedding']
    if any(model in model_path for model in embedding_models):
        llm_exporter = EmbeddingExporter(args)
    else:
        llm_exporter = LlmExporter(args)

    # some actions
    if args.test is not None:
        llm_exporter.response(args.test)

    if args.export is not None:
        llm_exporter.export(args.export)

if __name__ == '__main__':
    main()
