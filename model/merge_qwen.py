import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.models.qwen2_moe.modeling_qwen2_moe import * 
from tqdm import tqdm


def cal_scale_inv(svd_scale):
    try:
        scale_inv = torch.linalg.inv(svd_scale.to(torch.float32))
    except Exception as e:
        print("Warning: svd_scale is not full rank!")
        svd_scale += 1e-6 * torch.eye(svd_scale.shape[0]).to(svd_scale.device)
        scale_inv = torch.linalg.inv(svd_scale)
    return scale_inv.float()

from config import cfg
from .pruning_module import HiddenRepresentationPruning
from hf.utils import generate_probe, check_nan_inf, get_next_layer  



class Merge_QwenMoE(nn.Module):
    def __init__(self, config, share_ratio, delta_ratio, expert_freq, delta_share_V=False, delta_share_U=False, merge_method="freq", shared_infer=False):
        super().__init__()
        self.config = config
        self.norm_topk_prob = config.norm_topk_prob
        
        self.top_k = config.num_experts_per_tok
        self.num_experts = config.num_experts

        self.dtype = torch.bfloat16

        # added
        self.share_ratio = share_ratio
        self.delta_ratio = delta_ratio
        self.expert_freq = expert_freq

        self.delta_share_V = delta_share_V
        self.delta_share_U = delta_share_U
        self.merge_method = merge_method
        self.shared_infer = shared_infer

        # gating
        self.gate = nn.Linear(config.hidden_size, config.num_experts, bias=False)

        self.shared_expert = Qwen2MoeMLP(config, intermediate_size=config.shared_expert_intermediate_size)
        self.shared_expert_gate = torch.nn.Linear(config.hidden_size, 1, bias=False)


        self.hidden_size = config.hidden_size
        self.intermediate_size = config.moe_intermediate_size

        self.hidden_dim = config.hidden_size
        self.intermediate_dim = config.moe_intermediate_size


        self.Wmean_gate = nn.Linear(self.hidden_size, self.intermediate_size, bias=False, dtype=self.dtype)
        self.Wmean_down = nn.Linear(self.intermediate_size, self.hidden_size, bias=False, dtype=self.dtype)
        self.Wmean_up = nn.Linear(self.hidden_size, self.intermediate_size, bias=False, dtype=self.dtype)


        if self.delta_share_V == False and self.delta_share_U == False:
            self.experts = nn.ModuleList([meanW_deltaUV(config, self.Wmean_gate, self.Wmean_down, self.Wmean_up, 
                                                    self.delta_ratio, delta_share_V=False, delta_share_U=False)  for _ in range(self.num_experts)])
            
        elif self.delta_share_V == True and self.delta_share_U == False:
            if self.delta_ratio != 0:
                delta_low_rank = int(self.intermediate_dim * self.hidden_dim * self.delta_ratio / (self.intermediate_dim + self.hidden_dim))
                self.experts_delta_v1_shared = nn.Linear(self.hidden_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
                self.experts_delta_v2_shared = nn.Linear(self.intermediate_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
                self.experts_delta_v3_shared = nn.Linear(self.hidden_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
            else:
                self.experts_delta_v1_shared = None
                self.experts_delta_v2_shared = None
                self.experts_delta_v3_shared = None

            self.experts = nn.ModuleList([meanW_deltaUV(config, self.Wmean_gate, self.Wmean_down, self.Wmean_up, 
                                                    self.delta_ratio, delta_share_V=True, delta_share_U=False, 
                                                    experts_delta_v1_shared=self.experts_delta_v1_shared, 
                                                    experts_delta_v2_shared=self.experts_delta_v2_shared, experts_delta_v3_shared=self.experts_delta_v3_shared)  for _ in range(self.num_experts)])
            
        elif self.delta_share_V == True and self.delta_share_U == True:
            if self.delta_ratio != 0:
                delta_low_rank = int(self.intermediate_dim * self.hidden_dim * self.delta_ratio / (self.intermediate_dim + self.hidden_dim))
                self.experts_delta_u1_shared = nn.Linear(delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
                self.experts_delta_v1_shared = nn.Linear(self.hidden_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
                self.experts_delta_u2_shared = nn.Linear(delta_low_rank, self.hidden_dim, bias=False, dtype=torch.bfloat16)
                self.experts_delta_v2_shared = nn.Linear(self.intermediate_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
                self.experts_delta_u3_shared = nn.Linear(delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
                self.experts_delta_v3_shared = nn.Linear(self.hidden_dim, delta_low_rank, bias=False, dtype=torch.bfloat16)
            else:
                self.experts_delta_u1_shared = None
                self.experts_delta_v1_shared = None
                self.experts_delta_u2_shared = None
                self.experts_delta_v2_shared = None
                self.experts_delta_u3_shared = None
                self.experts_delta_v3_shared = None

            self.experts = nn.ModuleList([meanW_deltaUV(config, self.Wmean_gate, self.Wmean_down, self.Wmean_up, 
                                                    self.delta_ratio, delta_share_V=True, delta_share_U=True, experts_delta_u1_shared=self.experts_delta_u1_shared, experts_delta_v1_shared=self.experts_delta_v1_shared, 
                                                    experts_delta_u2_shared=self.experts_delta_u2_shared, experts_delta_v2_shared=self.experts_delta_v2_shared, 
                                                    experts_delta_u3_shared=self.experts_delta_u3_shared, experts_delta_v3_shared=self.experts_delta_v3_shared)  for _ in range(self.num_experts)])


    def update_Wmean(self):
        for i in range(self.num_experts):
            self.experts[i].Wmean_gate = self.Wmean_gate
            self.experts[i].Wmean_down = self.Wmean_down
            self.experts[i].Wmean_up = self.Wmean_up



    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """ """
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.view(-1, hidden_dim)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits = self.gate(hidden_states)

        routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
        if self.norm_topk_prob:
            routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
        # we cast back to the input dtype
        routing_weights = routing_weights.to(hidden_states.dtype)

        final_hidden_states = torch.zeros(
            (batch_size * sequence_length, hidden_dim), dtype=hidden_states.dtype, device=hidden_states.device
        )

        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = torch.nn.functional.one_hot(selected_experts, num_classes=self.num_experts).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.num_experts):
            expert_layer = self.experts[expert_idx]
            idx, top_x = torch.where(expert_mask[expert_idx])

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states[None, top_x].reshape(-1, hidden_dim)
            current_hidden_states = expert_layer(current_state) * routing_weights[top_x, idx, None]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states.to(hidden_states.dtype))

        shared_expert_output = self.shared_expert(hidden_states)
        shared_expert_output = F.sigmoid(self.shared_expert_gate(hidden_states)) * shared_expert_output

        final_hidden_states = final_hidden_states + shared_expert_output

        final_hidden_states = final_hidden_states.reshape(batch_size, sequence_length, hidden_dim)
        return final_hidden_states, router_logits

    @staticmethod
    @torch.no_grad()
    def svd_delta(W, ratio=1, svd_scale=None, rank=None, absorb_u=False, absorb_v=False, scale_type='svdllm'):
        if rank is None:
            num_s_after_trunc = int(W.shape[0] * W.shape[1] * ratio / (W.shape[0] + W.shape[1]))
        else:
            num_s_after_trunc = rank

        def safe_svd(matrix):
            try:
                return torch.linalg.svd(matrix, full_matrices=False)
            except torch._C._LinAlgError:
                # 添加小的扰动以提高数值稳定性
                eps = 1e-6
                noise = torch.randn_like(matrix) * eps
                matrix = matrix + noise
                try:
                    return torch.linalg.svd(matrix, full_matrices=False)
                except torch._C._LinAlgError:
                    # 如果还是失败，尝试更大的扰动
                    eps = 1e-2
                    noise = torch.randn_like(matrix) * eps
                    matrix = matrix + noise
                    try:
                        return torch.linalg.svd(matrix, full_matrices=False)
                    except torch._C._LinAlgError:
                        eps = 1
                        noise = torch.randn_like(matrix) * eps
                        matrix = matrix + noise
                        try:
                            return torch.linalg.svd(matrix, full_matrices=False)
                        except torch._C._LinAlgError:
                            raise ValueError("SVD failed after multiple attempts")

        if svd_scale is None:
            U, S, VT = safe_svd(W.float())
            del W
            truc_s = S[:num_s_after_trunc]
            del S
            truc_u = U[:, :num_s_after_trunc]
            del U
            truc_v = VT[:num_s_after_trunc, :]
            del VT
            truc_sigma = torch.diag(truc_s)
            del truc_s
            sqrtSigma = torch.sqrt(truc_sigma)
            svd_u = torch.matmul(truc_u, sqrtSigma)
            svd_v = torch.matmul(sqrtSigma, truc_v)
        else:
            if scale_type == 'svdllm':
                W_scale = torch.matmul(W, svd_scale.bfloat16().to(W.device))
                U, S, VT = safe_svd(W_scale.float())
                del W_scale
                truc_s = S[:num_s_after_trunc]
                del S
                truc_u = U[:, :num_s_after_trunc]
                del U
                truc_v = torch.matmul(VT[:num_s_after_trunc, :], cal_scale_inv(svd_scale).to(W.device))
                del VT
                truc_sigma = torch.diag(truc_s)
                del truc_s
                if absorb_u:
                    svd_u = torch.matmul(truc_u, truc_sigma)
                    svd_v = truc_v
                elif absorb_v:
                    svd_u = truc_u
                    svd_v = torch.matmul(truc_sigma, truc_v)
                else:
                    sqrtSigma = torch.sqrt(truc_sigma)
                    svd_u = torch.matmul(truc_u, sqrtSigma)
                    svd_v = torch.matmul(sqrtSigma, truc_v)
            elif scale_type == 'asvd':
                alpha = 0.5
                svd_scale *= alpha
                svd_scale += 1e-6
                W_scale = W * svd_scale.to(W.device).view(1, -1)
                U, S, VT = safe_svd(W_scale.float())
                del W_scale
                truc_s = S[:num_s_after_trunc]
                del S
                truc_u = U[:, :num_s_after_trunc]
                del U
                VT = VT / svd_scale.to(W.device).view(-1, 1)
                truc_v = VT[:num_s_after_trunc, :]
                del VT
                truc_sigma = torch.diag(truc_s)
                del truc_s
                sqrtSigma = torch.sqrt(truc_sigma)
                svd_u = torch.matmul(truc_u, sqrtSigma)
                svd_v = torch.matmul(sqrtSigma, truc_v)

        return svd_u.to(torch.bfloat16), svd_v.to(torch.bfloat16)



    @torch.no_grad()
    def merge_experts(self, module, svd_scale = None, hessian = None, scale_type='svdllm', preprocess_method = None):
        self.gate.weight.data = module.gate.weight.data

        self.shared_expert.gate_proj.weight.data = module.shared_expert.gate_proj.weight.data
        self.shared_expert.up_proj.weight.data = module.shared_expert.up_proj.weight.data
        self.shared_expert.down_proj.weight.data = module.shared_expert.down_proj.weight.data

        self.shared_expert_gate.weight.data = module.shared_expert_gate.weight.data

        if self.merge_method == "freq":
            self.Wmean_gate.weight.data = sum([module.experts[i].gate_proj.weight * self.expert_freq[i] for i in range(self.num_experts)]) / sum(self.expert_freq)
            self.Wmean_down.weight.data = sum([module.experts[i].down_proj.weight * self.expert_freq[i] for i in range(self.num_experts)]) / sum(self.expert_freq)
            self.Wmean_up.weight.data = sum([module.experts[i].up_proj.weight * self.expert_freq[i] for i in range(self.num_experts)]) / sum(self.expert_freq)

        elif self.merge_method == "mean":
            self.Wmean_gate.weight.data = sum([module.experts[i].gate_proj.weight for i in range(self.num_experts)]) / self.num_experts
            self.Wmean_down.weight.data = sum([module.experts[i].down_proj.weight for i in range(self.num_experts)]) / self.num_experts
            self.Wmean_up.weight.data = sum([module.experts[i].up_proj.weight for i in range(self.num_experts)]) / self.num_experts
        elif self.merge_method == "fisher":
            scaling_factor = 1.0
            scaling_factors_ft_list = self.expert_freq

            fisher_scale_w_gate = [0]*self.num_experts
            fisher_scale_w_down = [0]*self.num_experts
            fisher_scale_w_up = [0]*self.num_experts

            for j in range(self.num_experts):
                base_name = f"mlp.experts.{j}."
                fisher_scale_w_gate[j] = hessian[base_name + "gate_proj"].to(module.experts[j].gate_proj.weight.device) * scaling_factors_ft_list[j]
                fisher_scale_w_down[j] = hessian[base_name + "down_proj"].to(module.experts[j].down_proj.weight.device) * scaling_factors_ft_list[j]
                fisher_scale_w_up[j] = hessian[base_name + "up_proj"].to(module.experts[j].up_proj.weight.device) * scaling_factors_ft_list[j]

            fisher_scale_w_gate = [fisher_scale_w_gate[j] / sum(fisher_scale_w_gate) for j in range(self.num_experts)]
            fisher_scale_w_down = [fisher_scale_w_down[j] / sum(fisher_scale_w_down) for j in range(self.num_experts)]
            fisher_scale_w_up = [fisher_scale_w_up[j] / sum(fisher_scale_w_up) for j in range(self.num_experts)]
                
            Wmean_gate = sum([module.experts[j].gate_proj.weight * fisher_scale_w_gate[j] for j in range(self.num_experts)])
            Wmean_down = sum([module.experts[j].down_proj.weight * fisher_scale_w_down[j] for j in range(self.num_experts)])
            Wmean_up = sum([module.experts[j].up_proj.weight * fisher_scale_w_up[j] for j in range(self.num_experts)])

            self.Wmean_gate.weight.data = Wmean_gate
            self.Wmean_down.weight.data = Wmean_down
            self.Wmean_up.weight.data = Wmean_up


        else:
            raise ValueError(f"wrong merge method {self.merge_method}!")


        if self.delta_ratio != 0:
            scale_gate_mean = None
            scale_down_mean = None
            scale_up_mean = None
            total_freq = 0
            if svd_scale is not None:
                for j in range(self.num_experts):
                    base_name = f"mlp.experts.{j}."
                    freq = self.expert_freq[j]  
                    total_freq += freq
                    if scale_gate_mean is None:
                        scale_gate_mean = svd_scale[base_name + "gate_proj"] * freq
                    else:
                        scale_gate_mean += svd_scale[base_name + "gate_proj"] * freq
                    if scale_down_mean is None:
                        scale_down_mean = svd_scale[base_name + "down_proj"] * freq
                    else:
                        scale_down_mean += svd_scale[base_name + "down_proj"] * freq
                    if scale_up_mean is None:
                        scale_up_mean = svd_scale[base_name + "up_proj"] * freq
                    else:
                        scale_up_mean += svd_scale[base_name + "up_proj"] * freq
                scale_gate_mean /= total_freq
                scale_down_mean /= total_freq
                scale_up_mean /= total_freq

            if self.delta_share_V == True and self.delta_share_U == False:
                delta_gate = []
                delta_down = []
                delta_up = []

                for j in tqdm(range(self.num_experts), desc="Merging experts", leave=False):
                    
                    delta_gate.append(module.experts[j].gate_proj.weight - self.Wmean_gate.weight)
                    delta_down.append(module.experts[j].down_proj.weight - self.Wmean_down.weight)
                    delta_up.append(module.experts[j].up_proj.weight - self.Wmean_up.weight)

                delta_gate = torch.stack(delta_gate, dim=0).reshape(-1, delta_gate[0].shape[1])
                delta_down = torch.stack(delta_down, dim=0).reshape(-1, delta_down[0].shape[1])
                delta_up = torch.stack(delta_up, dim=0).reshape(-1, delta_up[0].shape[1])

                if svd_scale is None:
                    delta_u1, shared_v1 = self.svd_delta(delta_gate, rank=self.experts[0].delta_low_rank)
                    delta_u2, shared_v2 = self.svd_delta(delta_down, rank=self.experts[0].delta_low_rank)
                    delta_u3, shared_v3 = self.svd_delta(delta_up, rank=self.experts[0].delta_low_rank)
                else:
                    delta_u1, shared_v1 = self.svd_delta(delta_gate, rank=self.experts[0].delta_low_rank, svd_scale=scale_gate_mean, scale_type=scale_type)
                    delta_u2, shared_v2 = self.svd_delta(delta_down, rank=self.experts[0].delta_low_rank, svd_scale=scale_down_mean, scale_type=scale_type)
                    delta_u3, shared_v3 = self.svd_delta(delta_up, rank=self.experts[0].delta_low_rank, svd_scale=scale_up_mean, scale_type=scale_type)

                shared_v1 = nn.Parameter(shared_v1)
                shared_v2 = nn.Parameter(shared_v2)
                shared_v3 = nn.Parameter(shared_v3)

                del delta_gate, delta_down, delta_up

                self.experts_delta_v1_shared.weight = shared_v1
                self.experts_delta_v2_shared.weight = shared_v2
                self.experts_delta_v3_shared.weight = shared_v3

                for j in tqdm(range(self.num_experts), desc="Merging experts", leave=False):

                    self.experts[j].delta_u1.weight.data = delta_u1[j * self.experts[j].delta_u1.weight.shape[0]:(j + 1) * self.experts[j].delta_u1.weight.shape[0], :]
                    self.experts[j].delta_u2.weight.data = delta_u2[j * self.experts[j].delta_u2.weight.shape[0]:(j + 1) * self.experts[j].delta_u2.weight.shape[0], :]
                    self.experts[j].delta_u3.weight.data = delta_u3[j * self.experts[j].delta_u3.weight.shape[0]:(j + 1) * self.experts[j].delta_u3.weight.shape[0], :]


            if self.delta_share_V == True and self.delta_share_U == True:
                delta_gate = []
                delta_down = []
                delta_up = []

                for j in tqdm(range(self.num_experts), desc="Merging experts", leave=False):               
                    delta_gate.append(module.experts[j].gate_proj.weight - self.Wmean_gate.weight)
                    delta_down.append(module.experts[j].down_proj.weight - self.Wmean_down.weight)
                    delta_up.append(module.experts[j].up_proj.weight - self.Wmean_up.weight)

                delta_gate = torch.stack(delta_gate, dim=0).reshape(-1, delta_gate[0].shape[1])
                delta_down = torch.stack(delta_down, dim=0).reshape(-1, delta_down[0].shape[1])
                delta_up = torch.stack(delta_up, dim=0).reshape(-1, delta_up[0].shape[1])

                if svd_scale is None:
                    delta_u1, shared_v1 = self.svd_delta(delta_gate, rank=self.experts[0].delta_low_rank)
                    delta_u2, shared_v2 = self.svd_delta(delta_down, rank=self.experts[0].delta_low_rank)
                    delta_u3, shared_v3 = self.svd_delta(delta_up, rank=self.experts[0].delta_low_rank)
                else:
                    delta_u1, shared_v1 = self.svd_delta(delta_gate, rank=self.experts[0].delta_low_rank, svd_scale=scale_gate_mean, scale_type=scale_type)
                    delta_u2, shared_v2 = self.svd_delta(delta_down, rank=self.experts[0].delta_low_rank, svd_scale=scale_down_mean, scale_type=scale_type)
                    delta_u3, shared_v3 = self.svd_delta(delta_up, rank=self.experts[0].delta_low_rank, svd_scale=scale_up_mean, scale_type=scale_type)

                shared_v1 = nn.Parameter(shared_v1)
                shared_v2 = nn.Parameter(shared_v2)
                shared_v3 = nn.Parameter(shared_v3)

                shared_u1 = None
                shared_u2 = None
                shared_u3 = None
                total_freq = 0
                for j in range(self.num_experts):
                    freq = self.expert_freq[j]
                    total_freq += freq
                    if shared_u1 is None:
                        shared_u1 = delta_u1[j * self.experts[j].delta_u1.weight.shape[0]:(j + 1) * self.experts[j].delta_u1.weight.shape[0], :] * freq 
                    else:
                        shared_u1 += delta_u1[j * self.experts[j].delta_u1.weight.shape[0]:(j + 1) * self.experts[j].delta_u1.weight.shape[0], :] * freq
                    if shared_u2 is None:
                        shared_u2 = delta_u2[j * self.experts[j].delta_u2.weight.shape[0]:(j + 1) * self.experts[j].delta_u2.weight.shape[0], :] * freq
                    else:
                        shared_u2 += delta_u2[j * self.experts[j].delta_u2.weight.shape[0]:(j + 1) * self.experts[j].delta_u2.weight.shape[0], :] * freq
                    if shared_u3 is None:
                        shared_u3 = delta_u3[j * self.experts[j].delta_u3.weight.shape[0]:(j + 1) * self.experts[j].delta_u3.weight.shape[0], :] * freq
                    else:
                        shared_u3 += delta_u3[j * self.experts[j].delta_u3.weight.shape[0]:(j + 1) * self.experts[j].delta_u3.weight.shape[0], :] * freq
                shared_u1 /= total_freq
                shared_u2 /= total_freq
                shared_u3 /= total_freq

                shared_u1 = nn.Parameter(shared_u1)
                shared_u2 = nn.Parameter(shared_u2)
                shared_u3 = nn.Parameter(shared_u3)

                self.experts_delta_u1_shared.weight = shared_u1
                self.experts_delta_v1_shared.weight = shared_v1
                self.experts_delta_u2_shared.weight = shared_u2
                self.experts_delta_v2_shared.weight = shared_v2
                self.experts_delta_u3_shared.weight = shared_u3
                self.experts_delta_v3_shared.weight = shared_v3

            if self.delta_share_V == False and self.delta_share_U == False:
                for j in tqdm(range(self.num_experts), desc="Merging experts", leave=False):
                    delta_gate = (module.experts[j].gate_proj.weight - self.Wmean_gate.weight)
                    delta_down = (module.experts[j].down_proj.weight - self.Wmean_down.weight)
                    delta_up = (module.experts[j].up_proj.weight - self.Wmean_up.weight)

                    if svd_scale is not None:
                        base_name = f"mlp.experts.{j}."
                        self.experts[j].delta_u1.weight.data, self.experts[j].delta_v1.weight.data = self.svd_delta(delta_gate, ratio=self.delta_ratio, svd_scale=svd_scale[base_name + "gate_proj"], scale_type=scale_type)
                        self.experts[j].delta_u2.weight.data, self.experts[j].delta_v2.weight.data = self.svd_delta(delta_down, ratio=self.delta_ratio, svd_scale=svd_scale[base_name + "down_proj"], scale_type=scale_type)
                        self.experts[j].delta_u3.weight.data, self.experts[j].delta_v3.weight.data = self.svd_delta(delta_up, ratio=self.delta_ratio, svd_scale=svd_scale[base_name + "up_proj"], scale_type=scale_type)
                    else:
                        self.experts[j].delta_u1.weight.data, self.experts[j].delta_v1.weight.data = self.svd_delta(delta_gate, ratio=self.delta_ratio)
                        self.experts[j].delta_u2.weight.data, self.experts[j].delta_v2.weight.data = self.svd_delta(delta_down, ratio=self.delta_ratio)
                        self.experts[j].delta_u3.weight.data, self.experts[j].delta_v3.weight.data = self.svd_delta(delta_up, ratio=self.delta_ratio)
            
            del svd_scale




class meanW_deltaUV(nn.Module):
    def __init__(self, config, Wmean_gate, Wmean_down, Wmean_up, delta_ratio=1, delta_share_V=False, delta_share_U=False, 
                 experts_delta_u1_shared=None, experts_delta_v1_shared=None, 
                 experts_delta_u2_shared=None, experts_delta_v2_shared=None, 
                 experts_delta_u3_shared=None, experts_delta_v3_shared=None, 
                 shared_infer=False, layer_order=[]):
        super().__init__()
        self.intermediate_dim = config.moe_intermediate_size
        self.hidden_dim = config.hidden_size
        
        self.dtype = torch.bfloat16
        self.delta_share_V = delta_share_V
        self.delta_share_U = delta_share_U
        self.shared_infer = shared_infer

        self.Wmean_gate = Wmean_gate
        self.Wmean_down = Wmean_down
        self.Wmean_up = Wmean_up
            
        self.delta_ratio = delta_ratio

        self.act_fn = ACT2FN[config.hidden_act]

        if delta_share_V == False and delta_share_U == False and delta_ratio != 0:
            self.delta_low_rank = int(self.intermediate_dim * self.hidden_dim * self.delta_ratio / (self.intermediate_dim + self.hidden_dim))
            self.delta_u1 = nn.Linear(self.delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v1 = nn.Linear(self.hidden_dim, self.delta_low_rank, bias=False, dtype=torch.bfloat16)
            self.delta_u2 = nn.Linear(self.delta_low_rank, self.hidden_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v2 = nn.Linear(self.intermediate_dim, self.delta_low_rank, bias=False, dtype=torch.bfloat16)
            self.delta_u3 = nn.Linear(self.delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v3 = nn.Linear(self.hidden_dim, self.delta_low_rank, bias=False, dtype=torch.bfloat16)

        elif delta_share_V == True and delta_share_U == False and delta_ratio != 0:
            self.delta_low_rank = int(self.intermediate_dim * self.hidden_dim * self.delta_ratio / (self.intermediate_dim + self.hidden_dim))
            self.delta_u1 = nn.Linear(self.delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v1 = experts_delta_v1_shared
            self.delta_u2 = nn.Linear(self.delta_low_rank, self.hidden_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v2 = experts_delta_v2_shared
            self.delta_u3 = nn.Linear(self.delta_low_rank, self.intermediate_dim, bias=False, dtype=torch.bfloat16)
            self.delta_v3 = experts_delta_v3_shared
        # --------------------------------------------------
        self.layer_order = layer_order
        self.pruning_module = HiddenRepresentationPruning(cfg, f'Qwen_mlp_{layer_order}', config)
        self.probe_out_dim_indices = None
        # --------------------------------------------------
        

    def probe_process(self, x, **kwargs):
        # 1. generate probeW
        # 2. run matrix multiplication
        # 3. calculate score
        # 4. extract metric

        # generate probe
        # rank / mean / absnml
        # cur_batch_seq_len = x.size(1)
        cur_batch_seq_len = x.size(0)
        if cfg['gate_probe_ratio'] == cfg['up_probe_ratio']:
            # if 'respick' in cfg['prune_method']:
            #     residual_for_probe = kwargs['respick']
            # else:
            #     residual_for_probe = None
            residual_for_probe = None
            probe, seq_selected_indices = generate_probe(x, cfg['gate_probe_ratio'], residual_for_probe)
        else:
            raise ValueError('gate_probe_num should be equal to up_probe_num for now')
        
        # probe_out = self.act_fn(self.Wmean_gate(probe, cal_mlp_probe_out_dim_metric=True) + self.delta_u1(self.delta_v1(probe)) ) * \
        #       (self.Wmean_up(probe, cal_mlp_probe_out_dim_metric=True) + self.delta_u3(self.delta_v3(probe)))
        probe_out = self.act_fn( self.Wmean_gate(probe, cal_mlp_probe_out_dim_metric=True) ) * \
              (self.Wmean_up(probe, cal_mlp_probe_out_dim_metric=True) ) + self.act_fn(self.delta_u1(self.delta_v1(probe))) * \
              (self.delta_u3(self.delta_v3(probe)))
        
        # calculate score
        if 'calib' in cfg['prune_method'] or 'runningmean' in cfg['prune_method'] or 'ema' in cfg['prune_method']:
            probe_out_dim_metric = self.pruning_module.cal_mlp_prune_metric(probe_out, self.Wmean_down.weight.data + torch.matmul(self.delta_u2.weight.data, self.delta_v2.weight.data), cfg['prune_metric'], seq_selected_indices, global_metric_score_distribution=self.Wmean_down.get_global_metric_score_distribution(cur_batch_seq_len))
        else:
            probe_out_dim_metric = self.pruning_module.cal_mlp_prune_metric(probe_out, self.Wmean_down.weight.data + torch.matmul(self.delta_u2.weight.data, self.delta_v2.weight.data), cfg['prune_metric'], seq_selected_indices)

        if 'flapratio' in cfg['prune_method']:
            probe_out_dim_indices, prune_out_dim_indices = self.pruning_module.sort_mlp_metric(probe_out_dim_metric, cfg['tc_multiple'], pruning_ratio=self.Wmean_down.pruning_ratio)
        else:
            probe_out_dim_indices, prune_out_dim_indices = self.pruning_module.sort_mlp_metric(probe_out_dim_metric, cfg['tc_multiple'])

        # extract matrix
        self.Wmean_gate.prepare_async_weight(out_dim_indices=probe_out_dim_indices)
        self.Wmean_up.prepare_async_weight(out_dim_indices=probe_out_dim_indices)
        self.Wmean_down.prepare_async_weight(in_dim_indices=probe_out_dim_indices)
        return probe_out_dim_indices, probe_out
    


    def forward(self, x, current_up_hidden_states = None, current_gate_hidden_states = None, current_down_hidden_states = None,
                current_up_deltav_hidden_states = None, current_gate_deltav_hidden_states = None, current_down_deltav_hidden_states = None,
                shared_infer=False, **kwargs):
        if cfg['test_stage'] == True:
            if self.delta_ratio == 0:
                if shared_infer == False:
                    up = self.Wmean_up(x)
                    gate = self.Wmean_gate(x)
                    return self.Wmean_down(self.act_fn(gate) * up)
                else:
                    return self.Wmean_down(self.act_fn(current_gate_hidden_states) * current_up_hidden_states)

            if shared_infer == False:
                up = self.Wmean_up(x) + self.delta_u3(self.delta_v3(x))
                gate = self.Wmean_gate(x) + self.delta_u1(self.delta_v1(x))
                down_proj = self.Wmean_down(self.act_fn(gate) * up) + self.delta_u2(self.delta_v2(self.act_fn(gate) * up))
            else:
                if self.delta_share_V == True:
                    up = current_up_hidden_states + self.delta_u3(current_up_deltav_hidden_states)
                    gate = current_gate_hidden_states + self.delta_u1(current_gate_deltav_hidden_states)
                    return self.Wmean_down(self.act_fn(gate) * up) + self.delta_u2(self.delta_v2(self.act_fn(gate) * up))
                else:
                    up = current_up_hidden_states + self.delta_u3(self.delta_v3(x))
                    gate = current_gate_hidden_states + self.delta_u1(self.delta_v1(x))
                    return self.Wmean_down(self.act_fn(gate) * up) + self.delta_u2(self.delta_v2(self.act_fn(gate) * up))

            return down_proj
        elif cfg['no_probe_process'] == True:
            if self.delta_ratio == 0:
                if shared_infer == False:
                    up = self.Wmean_up(x)
                    gate = self.Wmean_gate(x)
                    return self.Wmean_down(self.act_fn(gate) * up)
                else:
                    return current_down_hidden_states
            if self.layer_order not in cfg['skip_layers']:
                if 'probe' in cfg['prune_method']:
                    probe_out_dim_indices = None
                    if cfg['mode'] == 'sync':
                        # probe_out_dim_indices, probe_out = self.probe_process(x, **kwargs)
                        keep_dim = int(self.hidden_dim * (1 - cfg['prune_ratio']))
                        probe_out_dim_indices = torch.arange(keep_dim)
                        # count flops for probe
                        if cfg['onlyprobe'] == True:
                            # match the shape, and will not count the flops for this part
                            down_proj = torch.zeros((cfg['batch_size'], x.shape[1], self.hidden_size), device=x.device, dtype=x.dtype)
                            return down_proj
                    elif cfg['mode'] == 'asyncintra':
                        if 'post_layernorm_attn_residual' in kwargs:
                            # _, _ = self.probe_process(kwargs['post_layernorm_attn_residual'], **kwargs)
                            pass
                        else:
                            pass

                    if 'recordcommonchannel' in cfg['prune_method']:
                        self.mlp_cur_select_indices = probe_out_dim_indices.tolist()

                    if self.delta_ratio == 0:
                        if shared_infer == False:
                            up = self.Wmean_up(x)
                            gate = self.Wmean_gate(x)
                            return self.Wmean_down(self.act_fn(gate) * up)
                        else:
                            return current_down_hidden_states
                    if shared_infer == False:
                        up = self.Wmean_up(x, out_dim_indices=probe_out_dim_indices) + self.delta_u3(self.delta_v3(x))[:, probe_out_dim_indices]
                        gate = self.Wmean_gate(x, out_dim_indices=probe_out_dim_indices) + self.delta_u1(self.delta_v1(x))[:, probe_out_dim_indices]
                        main_down_proj = self.Wmean_down(self.act_fn(gate) * up, in_dim_indices=probe_out_dim_indices)
                        delta_down_proj = self.delta_u2(F.linear( (self.act_fn(gate) * up), self.delta_v2.weight[:, probe_out_dim_indices], bias=None))
                        down_proj = main_down_proj + delta_down_proj
                    else:
                        if self.delta_share_V == True:
                            up = current_up_hidden_states + self.delta_u3(current_up_deltav_hidden_states)[:, probe_out_dim_indices]
                            gate = current_gate_hidden_states + self.delta_u1(current_gate_deltav_hidden_states)[:, probe_out_dim_indices]
                            return current_down_hidden_states + self.delta_u2(current_down_deltav_hidden_states)
                        else:
                            up = current_up_hidden_states + self.delta_u3(self.delta_v3(x))[:, probe_out_dim_indices]
                            gate = current_gate_hidden_states + self.delta_u1(self.delta_v1(x))[:, probe_out_dim_indices]
                            return current_down_hidden_states + self.delta_u2(F.linear( (self.act_fn(gate) * up), self.delta_v2.weight[:, probe_out_dim_indices], bias=None))
                    return down_proj
        elif cfg['calibration_stage'] == True:
            up = self.Wmean_up(x) + self.delta_u3(self.delta_v3(x))
            gate = self.Wmean_gate(x) + self.delta_u1(self.delta_v1(x))
            down_proj = self.Wmean_down(self.act_fn(gate) * up) + self.delta_u2(self.delta_v2(self.act_fn(gate) * up))
            return down_proj
        elif cfg['calibration_stage'] == False:
            if self.layer_order not in cfg['skip_layers']:
                if 'probe' in cfg['prune_method']:
                    probe_out_dim_indices = None
                    if cfg['mode'] == 'sync':
                        probe_out_dim_indices, probe_out = self.probe_process(x, **kwargs)
                        # count flops for probe
                        if cfg['onlyprobe'] == True:
                            # match the shape, and will not count the flops for this part
                            down_proj = torch.zeros((cfg['batch_size'], x.shape[1], self.hidden_size), device=x.device, dtype=x.dtype)
                            return down_proj
                    elif cfg['mode'] == 'asyncintra':
                        if 'post_layernorm_attn_residual' in kwargs:
                            _, _ = self.probe_process(kwargs['post_layernorm_attn_residual'], **kwargs)
                        else:
                            pass

                    if 'recordcommonchannel' in cfg['prune_method']:
                        self.mlp_cur_select_indices = probe_out_dim_indices.tolist()

                    up = self.Wmean_up(x, out_dim_indices=probe_out_dim_indices) + self.delta_u3(self.delta_v3(x))[:, probe_out_dim_indices]
                    gate = self.Wmean_gate(x, out_dim_indices=probe_out_dim_indices) + self.delta_u1(self.delta_v1(x))[:, probe_out_dim_indices]
                    main_down_proj = self.Wmean_down(self.act_fn(gate) * up, in_dim_indices=probe_out_dim_indices)
                    delta_down_proj = self.delta_u2(F.linear( (self.act_fn(gate) * up), self.delta_v2.weight[:, probe_out_dim_indices], bias=None))
                    down_proj = main_down_proj + delta_down_proj
                    return down_proj
            else:
                up = self.Wmean_up(x) + self.delta_u3(self.delta_v3(x))
                gate = self.Wmean_gate(x) + self.delta_u1(self.delta_v1(x))
                down_proj = self.Wmean_down(self.act_fn(gate) * up) + self.delta_u2(self.delta_v2(self.act_fn(gate) * up))
                return down_proj
