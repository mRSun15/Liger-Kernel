import pytest
import torch
import torch.nn.functional as F

from liger_kernel.chunked_loss.grpo_loss import LigerFusedLinearGRPOFunction
from liger_kernel.utils import infer_device
from test.utils import assert_verbose_allclose
from test.utils import set_seed

device = infer_device()

# set random seed globally
set_seed()
# reset torch compiler cache
torch.compiler.reset()

class TorchLMHeadGRPO(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ref_bias: bool = False,
        beta: float = 0.1,
        epsilon_low: float = 0.2,
        epsilon_high: float = 0.2,
        temperature: float = 1.0,
        use_ref_model: bool = True,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(in_features=H, out_features=V, bias=bias, dtype=dtype)
        self.ref_lin = torch.nn.Linear(in_features=H, out_features=V, bias=ref_bias, dtype=dtype)
        self.beta = beta
        self.epsilon_low = epsilon_low
        self.epsilon_high = epsilon_high
        self.temperature = temperature
        self.use_ref_model = use_ref_model
        
    def forward(
        self,
        x,  # Shape: [batch_size, seq_len, hidden_size]
        attention_mask,  # Shape: [batch_size, seq_len]
        advantages,  # Shape: [batch_size,]
        ref_input=None,  # Shape: [batch_size, seq_len, hidden_size]
        old_per_token_logps=None,
    ):
        logits = (x @ self.lin.weight.t())
        if self.lin.bias is not None:
            logits = logits + self.lin.bias
        if self.temperature != 1.0:
            logits = logits / self.temperature
        # Get log probabilities
        log_probs = F.log_softmax(logits, dim=-1)

        # Get chosen token probabilities
        chosen_tokens = log_probs.argmax(dim=-1)
        chosen_token_logprobs = log_probs.gather(dim=-1, index=chosen_tokens.unsqueeze(-1)).squeeze(-1)

        # Get reference model probabilities
        if self.use_ref_model:
            with torch.no_grad():
                ref_logits = ref_input @ self.ref_lin.weight.t()
                if self.ref_lin.bias is not None:
                    ref_logits = ref_logits + self.ref_lin.bias
                if self.temperature != 1.0:
                    ref_logits = ref_logits / self.temperature
                ref_log_probs = F.log_softmax(ref_logits, dim=-1)
                ref_token_logprobs = ref_log_probs.gather(dim=-1, index=chosen_tokens.unsqueeze(-1)).squeeze(-1)
        else:
            ref_token_logprobs = chosen_token_logprobs.detach()


        # Compute policy gradient loss with importance sampling ratio
        old_per_token_logps = old_per_token_logps if old_per_token_logps is not None else chosen_token_logprobs.detach()
        coef_1 = torch.exp(chosen_token_logprobs - old_per_token_logps)
        coef_2 = torch.clamp(coef_1, 1 - self.epsilon_low, 1 + self.epsilon_high)
        per_token_loss1 = coef_1 * advantages.unsqueeze(1)
        per_token_loss2 = coef_2 * advantages.unsqueeze(1)
        per_token_loss = -torch.min(per_token_loss1, per_token_loss2)
        if self.beta != 0.0:
            # Compute KL divergence between model and reference model
            kl_div = (
                torch.exp(ref_token_logprobs - chosen_token_logprobs) - (ref_token_logprobs - chosen_token_logprobs) - 1.0
            )
            per_token_loss = per_token_loss + self.beta * kl_div

        # Apply masking and normalize
        loss = (per_token_loss * attention_mask).sum() / torch.clamp(attention_mask.sum(), min=1.0)

        # Compute metrics
        metrics = [
            chosen_token_logprobs.mean(),
            log_probs.mean(),
        ]
        if self.beta != 0.0:
            metrics.append(
                ((kl_div * attention_mask).sum(dim=1) / torch.clamp(attention_mask.sum(dim=1), min=1.0)).mean()
            )

        return loss, metrics


class LigerLMHeadGRPO(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ref_bias: bool = False,
        beta: float = 0.1,
        epsilon_low: float = 0.2,
        epsilon_high: float = 0.2,
        temperature: float = 1.0,
        use_ref_model: bool = True,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(in_features=H, out_features=V, bias=bias, dtype=dtype)
        self.ref_lin = torch.nn.Linear(in_features=H, out_features=V, bias=ref_bias, dtype=dtype)
        self.grpo_loss = LigerFusedLinearGRPOFunction.apply
        self.beta = beta
        self.epsilon_low = epsilon_low
        self.epsilon_high = epsilon_high
        self.temperature = temperature
        self.use_ref_model = use_ref_model

    def forward(
        self,
        x,
        attention_mask,
        advantages,
        ref_input=None,
        old_per_token_logps=None,
    ):
        # Pass only the arguments defined in LigerFusedLinearGRPOFunction.forward()
        return self.grpo_loss(
            x,  # _input
            self.lin.weight,  # weight
            attention_mask,  # attention_mask
            advantages,  # advantages
            self.lin.bias,  # bias
            ref_input,  # ref_input
            self.ref_lin.weight,  # ref_weight
            self.ref_lin.bias,  # ref_bias
            old_per_token_logps,  # old_per_token_logps
            self.beta,  # beta
            self.epsilon_low,  # epsilon_low
            self.epsilon_high,  # epsilon_high
            self.temperature,  # temperature
            True,  # compiled
            self.use_ref_model,  # use_ref_model
        )


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (3, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-2, 5e-2),
        (1.0, torch.float32, 1e-4, 5e-3),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize("ref_bias", [True, False])
@pytest.mark.parametrize(
    "beta, epsilon_low, epsilon_high, temperature",
    [
        # Standard settings
        (0.1, 0.2, 0.2, 1.0),
        (0.0, 0.1, 0.1, 2.0),
    ]
)
@pytest.mark.parametrize("use_ref_model", [True, False])
@pytest.mark.parametrize("old_per_token_logps", [True, False])
def test_correctness(
    B,
    T,
    H,
    V,
    scalar,
    dtype,
    atol,
    rtol,
    bias,
    ref_bias,
    beta,
    epsilon_low,
    epsilon_high,
    temperature,
    use_ref_model,
    old_per_token_logps,
):
    torch_lm_head_grpo = TorchLMHeadGRPO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=ref_bias,
        beta=beta,
        epsilon_low=epsilon_low,
        epsilon_high=epsilon_high,
        temperature=temperature,
        use_ref_model=use_ref_model,
    )
    liger_lm_head_grpo = LigerLMHeadGRPO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=ref_bias,
        beta=beta,
        epsilon_low=epsilon_low,
        epsilon_high=epsilon_high,
        temperature=temperature,
        use_ref_model=use_ref_model,
    )

    # Initialize weights
    torch_lm_head_grpo.lin.weight.data = liger_lm_head_grpo.lin.weight.data = torch.randn(
        V, H, device=device, dtype=dtype
    )
    if bias:
        torch_lm_head_grpo.lin.bias.data = liger_lm_head_grpo.lin.bias.data = torch.randn(V, device=device, dtype=dtype)

    torch_lm_head_grpo.ref_lin.weight.data = liger_lm_head_grpo.ref_lin.weight.data = torch.randn(
        V, H, device=device, dtype=dtype
    )
    if ref_bias:
        torch_lm_head_grpo.ref_lin.bias.data = liger_lm_head_grpo.ref_lin.bias.data = torch.randn(V, device=device, dtype=dtype)

    # Create inputs with shape [B, T, H]
    _input = torch.randn(B, T, H, device=device, dtype=dtype) * scalar
    input1 = _input.detach().clone().requires_grad_(True)
    input2 = _input.detach().clone().requires_grad_(True)

    # Create attention mask with random padding [B, T]
    attention_mask = torch.ones(B, T, device=device)
    num_elements_to_mask = torch.randint(1, B * T // 2, (1,)).item()
    mask_indices = torch.randperm(B * T)[:num_elements_to_mask]
    attention_mask.view(-1)[mask_indices] = 0

    # Create advantages with shape [B]
    advantages = torch.rand(B, device=device, dtype=dtype)

    # Create reference inputs (optional) with shape [B, T, H]
    ref_input = torch.randn(B, T, H, device=device, dtype=dtype) * scalar

    if old_per_token_logps:
        old_per_token_logps = torch.randn(B, T, device=device, dtype=dtype) * scalar
    else:
        old_per_token_logps = None

    # Forward pass with reference model
    loss1, aux1 = torch_lm_head_grpo(
        input1, attention_mask, advantages, ref_input=ref_input, old_per_token_logps=old_per_token_logps
    )
    loss2, aux2 = liger_lm_head_grpo(
        input2, attention_mask, advantages, ref_input=ref_input, old_per_token_logps=old_per_token_logps
    )

    # Check losses match
    assert_verbose_allclose(loss1, loss2, atol=atol, rtol=rtol)

    # Check metrics match
    assert len(aux1) == len(aux2)
    for metric1, metric2 in zip(aux1, aux2):
        assert_verbose_allclose(metric1, metric2, atol=atol, rtol=rtol)

    # Backward pass
    loss1.backward()
    loss2.backward()

    # Check gradients match
    assert_verbose_allclose(input1.grad, input2.grad, atol=atol, rtol=rtol)
    assert_verbose_allclose(
        torch_lm_head_grpo.lin.weight.grad,
        liger_lm_head_grpo.lin.weight.grad,
        atol=atol,
        rtol=rtol,
    )
    if bias:
        assert_verbose_allclose(
            torch_lm_head_grpo.lin.bias.grad,
            liger_lm_head_grpo.lin.bias.grad,
            atol=atol,
            rtol=rtol,
        )
