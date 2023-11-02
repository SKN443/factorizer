from typing import Dict, Union, Optional, Sequence
import re
from functools import reduce

import torch
from torch import nn
from torch.nn.modules.utils import _ntuple
from einops.layers.torch import Rearrange
import opt_einsum as oe


def t(x: torch.Tensor) -> torch.Tensor:
    """Transpose a tensor, i.e. "b i j -> b j i"."""
    return x.transpose(-2, -1)


def dot(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    out = (x * y).flatten(-2).sum(-1, keepdim=True)
    return out


def norm2(x: torch.Tensor, w: Optional[torch.Tensor] = None) -> torch.Tensor:
    w = torch.ones_like(x) if w is None else w
    x = x.flatten(1)
    w = w.flatten(1)
    out = torch.sum(w * (x**2), dim=1).sqrt()
    return out


def error(
    x: torch.Tensor, y: torch.Tensor, w: Optional[torch.Tensor] = None
) -> torch.Tensor:
    return norm2(x - y, w)


def relative_error(
    x: torch.Tensor, y: torch.Tensor, w: Optional[torch.Tensor] = None, eps: float = 1e-16
) -> torch.Tensor:
    numerator = norm2(x - y, w)
    denominator = norm2(x, w)
    return numerator / (denominator + eps)


def cp(factor_matrices: Sequence[torch.Tensor]) -> torch.Tensor:
    args = []
    legs = ["b"]
    for m, u in enumerate(factor_matrices):
        args.append(u)
        args.append(["b", f"i{m}", "r"])
        legs.append(f"i{m}")

    args.append(legs)
    out = oe.contract(*args)
    return out


def khatri_rao(factor_matrices: Sequence[torch.Tensor]) -> torch.Tensor:
    args = []
    legs = ["b"]
    for m, u in enumerate(factor_matrices):
        args.append(u)
        args.append(["b", f"i{m}", "r"])
        legs.append(f"i{m}")

    legs.append("r")
    args.append(legs)
    out = oe.contract(*args)
    out = torch.flatten(out, start_dim=1, end_dim=-2)
    return out


class Reshape(nn.Module):
    def __init__(
        self,
        input_size: Sequence[int],
        equation: Optional[str] = None,
        shifts: Optional[Sequence[int]] = None,
        dims: Optional[Sequence[int]] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.input_size = input_size
        if equation is None:
            self.rearrange = nn.Identity()
            self.rearrange_inv = nn.Identity()
            self.output_size = input_size
        else:
            self.equation = equation
            left, right = equation.split("->")
            self.left = left = left.rstrip().lstrip()
            self.right = right = right.rstrip().lstrip()
            self.rearrange = Rearrange(equation, **kwargs)

            self.dim_lengths = self.infer_dims(self.left, input_size, kwargs)
            self.output_size = self.compute_size(self.right, self.dim_lengths)

            self.equation_inv = equation_inv = " -> ".join([right, left])
            self.rearrange_inv = Rearrange(equation_inv, **self.dim_lengths)

        if shifts is not None:
            self.shifts = shifts
            self.shifts_inv = tuple(-s for s in self.shifts)
            self.dims = dims

    @staticmethod
    def infer_dims(
        pattern: str, size: Sequence[Union[int, None]], dim_lengths: Dict[str, int]
    ) -> Dict[str, Union[int, None]]:
        # Extract all dimension groups from the pattern
        groups = re.findall(r"\(([^)]+)\)|(\w+)", pattern)

        inferred_dims = {}
        for group, s in zip(groups, size):
            # Flatten the group to a list of dimensions
            dims = group[0].split() if group[0] else [group[1]]

            # If size is None or not all dimensions are known, add only known dimensions to the result
            if s is None or len([d for d in dims if d in dim_lengths]) < len(dims) - 1:
                for d in dims:
                    if d in dim_lengths:
                        inferred_dims[d] = dim_lengths[d]
                continue

            # Calculate the product of known dimensions
            known_product = reduce(
                lambda x, y: x * y, (dim_lengths[d] for d in dims if d in dim_lengths), 1
            )

            # Infer the remaining dimension if possible
            unknown_dim = s // known_product
            for d in dims:
                inferred_dims[d] = dim_lengths.get(d, unknown_dim)

        return inferred_dims

    @staticmethod
    def compute_size(
        pattern: str, dim_lengths: Dict[str, int]
    ) -> Sequence[Union[int, None]]:
        # Extract all dimension groups from the pattern
        groups = re.findall(r"\(([^)]+)\)|(\w+)", pattern)

        sizes = []
        for group in groups:
            # Flatten the group to a list of dimensions
            dims = group[0].split() if group[0] else [group[1]]

            # If any dimension in the group is missing from dim_lengths, append None to sizes
            if any(d not in dim_lengths for d in dims):
                sizes.append(None)
            else:
                # Calculate the product of the dimensions and append to sizes
                group_size = reduce(lambda x, y: x * y, (dim_lengths[d] for d in dims))
                sizes.append(group_size)

        return tuple(sizes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if hasattr(self, "shifts"):
            x = torch.roll(x, self.shifts, self.dims)

        out = self.rearrange(x)
        return out

    def inverse_forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.rearrange_inv(x)
        if hasattr(self, "shifts"):
            out = torch.roll(out, self.shifts_inv, self.dims)

        return out


class Matricize(Reshape):
    """Matricize, e.g. 'b (c1 c2) (h1 h2) (w1 w2) -> (b c1) (h1 w1) c2 (h2 w2)'."""

    def __init__(
        self,
        input_size: Sequence[int],
        num_heads: Optional[int] = None,
        head_dim: Optional[int] = None,
        grid_size: Union[int, Sequence[int], None] = None,
        patch_size: Union[int, Sequence[int], None] = None,
        shifts: Union[int, Sequence[int], None] = None,
        **kwargs,
    ) -> None:
        assert (num_heads, head_dim) != (
            None,
            None,
        ), "'num_heads' or 'head_dim' must be specified."
        assert (grid_size, patch_size) != (
            None,
            None,
        ), "'grid_size' or 'kernel_size' must be specified."

        spatial_dim = len(input_size) - 2
        to_ntuple = _ntuple(spatial_dim)

        left = f'b (h d) {" ".join([f"(g{i} p{i})" for i in range(spatial_dim)])}'
        right = "(b h) "
        right += f'({" ".join([f"g{i}" for i in range(spatial_dim)])}) '
        right += f'd ({" ".join([f"p{i}" for i in range(spatial_dim)])})'
        equation = f"{left} -> {right}"

        dims_lengths = {}
        if num_heads is not None:
            dims_lengths["h"] = max(num_heads, 1)

        if head_dim is not None:
            dims_lengths["d"] = max(head_dim, 1)

        for j, g in enumerate(to_ntuple(grid_size)):
            if g is not None:
                dims_lengths[f"g{j}"] = max(g, 1)

        for j, p in enumerate(to_ntuple(patch_size)):
            if p is not None:
                dims_lengths[f"p{j}"] = max(p, 1)

        if shifts is not None:
            dims = tuple(j + 2 for j in range(spatial_dim))
            shifts = to_ntuple(shifts)
        else:
            dims = None

        super().__init__(
            input_size,
            equation=equation,
            shifts=shifts,
            dims=dims,
            **dims_lengths,
            **kwargs,
        )


class SWMatricize(nn.Module):
    def __init__(
        self,
        input_size: Sequence[int],
        num_heads: Optional[int] = None,
        head_dim: Optional[int] = None,
        grid_size: Optional[Sequence[int]] = None,
        patch_size: Optional[Sequence[int]] = None,
        shifts: Optional[Sequence[Union[Sequence[int], None]]] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        spatial_dim = len(input_size) - 2
        to_ntuple = _ntuple(spatial_dim)
        patch_size = to_ntuple(patch_size)
        grid_size = to_ntuple(grid_size)
        if shifts is None:
            shifts = [None, tuple(s // 2 for s in patch_size)]

        shifted_windows = []
        for s in shifts:
            shifted_windows.append(
                Matricize(
                    input_size,
                    num_heads=num_heads,
                    head_dim=head_dim,
                    grid_size=grid_size,
                    patch_size=patch_size,
                    shifts=s,
                    **kwargs,
                )
            )

        self.shifted_windows = nn.ModuleList(shifted_windows)
        self.output_size = self.shifted_windows[0].output_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = []
        for shifted_window in self.shifted_windows:
            out.append(shifted_window(x))
        return torch.cat(out)

    def inverse_forward(self, x: torch.Tensor) -> torch.Tensor:
        b = x.shape[0]
        num_shifts = len(self.shifted_windows)
        out = 0.0
        for j in range(num_shifts):
            slc = slice(j * (b // num_shifts), (j + 1) * (b // num_shifts))
            z = x[None, slc, ...][0]
            # instead of z = x[slc, ...] in order to make it work in case of MetaTensor
            out = out + self.shifted_windows[j].inverse_forward(z)

        out = out / num_shifts
        return out


class Tensorize(Reshape):
    """Tensorize, e.g. 'b (c1 c2) (h1 h2) (w1 w2) -> (b c1) (h1 w1) c2 h2 w2'."""

    def __init__(
        self,
        input_size: Sequence[int],
        num_heads: Optional[int] = None,
        head_dim: Optional[int] = None,
        grid_size: Optional[Sequence[int]] = None,
        patch_size: Optional[Sequence[int]] = None,
        shifts: Optional[Sequence[int]] = None,
        **kwargs,
    ) -> None:
        assert (num_heads, head_dim) != (
            None,
            None,
        ), "'num_heads' or 'head_dim' must be specified."
        assert (grid_size, patch_size) != (
            None,
            None,
        ), "'grid_size' or 'kernel_size' must be specified."

        spatial_dim = len(input_size) - 2
        to_ntuple = _ntuple(spatial_dim)

        left = f'b (h d) {" ".join([f"(g{i} p{i})" for i in range(spatial_dim)])}'
        right = "(b h) "
        right = f'({" ".join([f"g{i}" for i in range(spatial_dim)])}) '
        right += f'd {" ".join([f"p{i}" for i in range(spatial_dim)])}'
        equation = f"{left} -> {right}"

        dims_lengths = {}
        if num_heads is not None:
            dims_lengths["h"] = max(num_heads, 1)

        if head_dim is not None:
            dims_lengths["d"] = max(head_dim, 1)

        for j, g in enumerate(to_ntuple(grid_size)):
            if g is not None:
                dims_lengths[f"g{j}"] = max(g, 1)

        for j, p in enumerate(to_ntuple(patch_size)):
            if p is not None:
                dims_lengths[f"p{j}"] = max(p, 1)

        if shifts is not None:
            dims = tuple(j + 2 for j in range(spatial_dim))
            shifts = to_ntuple(shifts)
        else:
            dims = None

        super().__init__(
            input_size,
            equation=equation,
            shifts=shifts,
            dims=dims,
            **dims_lengths,
            **kwargs,
        )


class SWTensorize(nn.Module):
    def __init__(
        self,
        input_size: Sequence[int],
        num_heads: Optional[int] = None,
        head_dim: Optional[int] = None,
        grid_size: Optional[Sequence[int]] = None,
        patch_size: Optional[Sequence[int]] = None,
        shifts: Optional[Sequence[int]] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        spatial_dim = len(input_size) - 2
        to_ntuple = _ntuple(spatial_dim)
        patch_size = to_ntuple(patch_size)
        grid_size = to_ntuple(grid_size)
        if shifts is None:
            shifts = [None, tuple(s // 2 for s in patch_size)]

        shifted_windows = []
        for s in shifts:
            shifted_windows.append(
                Tensorize(
                    input_size,
                    num_heads=num_heads,
                    head_dim=head_dim,
                    grid_size=grid_size,
                    patch_size=patch_size,
                    shifts=s,
                    **kwargs,
                )
            )

        self.shifted_windows = nn.ModuleList(shifted_windows)
        self.output_size = self.shifted_windows[0].output_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = []
        for shifted_window in self.shifted_windows:
            out.append(shifted_window(x))
        return torch.cat(out)

    def inverse_forward(self, x: torch.Tensor) -> torch.Tensor:
        b = x.shape[0]
        num_shifts = len(self.shifted_windows)
        out = 0.0

        for j in range(num_shifts):
            slc = slice(j * (b // num_shifts), (j + 1) * (b // num_shifts))
            z = x[None, slc, ...][0]
            # instead of z = x[slc, ...] in order to make it work in case of MetaTensor
            out = out + self.shifted_windows[j].inverse_forward(z)

        out = out / num_shifts
        return out
