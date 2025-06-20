import re
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
from pytorch_lightning.utilities import rank_zero_warn
from torch import Tensor
from torch.autograd import grad
from torch_geometric.data import Data
from torch_scatter import scatter
from torch_cluster import radius_graph

from src import priors
from src.models import output_modules

from src.models.MABNet.mabnet import MABNet
def create_model(args, prior_model=None, mean=None, std=None):

    # representation network
    if args["model"] == "MABNet":
        is_equivariant = True
        model_args = dict(
            lmax=args["lmax"],
            vecnorm_type=args["vecnorm_type"],
            trainable_vecnorm=args["trainable_vecnorm"],
            num_heads=args["num_heads"],
            num_layers=args["num_layers"],
            hidden_channels=args["embedding_dimension"],
            num_rbf=args["num_rbf"],
            rbf_type=args["rbf_type"],
            trainable_rbf=args["trainable_rbf"],
            activation=args["activation"],
            attn_activation=args["attn_activation"],
            max_z=args["max_z"],
            cutoff=args["cutoff"],
            cutoff_pruning=args["cutoff_pruning"],
            max_num_neighbors=args["max_num_neighbors"],
            max_num_edges_save=args["max_num_edges_save"],
            use_padding=args["use_padding"],
            many_body=args["many_body"],
        )
        representation_model = MABNet(**model_args)
    else:
        raise ValueError(f"Unknown model {args['model']}.")
    
    # prior model
    if args["prior_model"] and prior_model is None:
        assert "prior_args" in args, (
            f"Requested prior model {args['prior_model']} but the "
            f'arguments are lacking the key "prior_args".'
        )
        assert hasattr(priors, args["prior_model"]), (
            f'Unknown prior model {args["prior_model"]}. '
            f'Available models are {", ".join(priors.__all__)}'
        )
        # instantiate prior model if it was not passed to create_model (i.e. when loading a model)
        prior_model = getattr(priors, args["prior_model"])(**args["prior_args"])

    # create output network
    if isinstance(representation_model, (MABNet)):
        output_prefix = "Equivariant" if is_equivariant else ""
        output_model = getattr(output_modules, output_prefix + args["output_model"])(args["embedding_dimension"], args["activation"])
    else:
        output_model = None

    model = MolDynModel(
        representation_model,
        output_model,
        prior_model=prior_model,
        reduce_op=args["reduce_op"],
        mean=mean,
        std=std,
        derivative=args["derivative"],
        cutoff=args["cutoff"],
        cutoff_pruning=args["cutoff_pruning"],
        max_num_neighbors=args["max_num_neighbors"],
        max_num_edges_save=args["max_num_edges_save"],
        many_body=args["many_body"],
    )
    return model


def load_model(filepath, args=None, device="cpu", **kwargs):
    ckpt = torch.load(filepath, map_location="cpu")
    if args is None:
        args = ckpt["hyper_parameters"]

    for key, value in kwargs.items():
        if not key in args:
            rank_zero_warn(f"Unknown hyperparameter: {key}={value}")
        args[key] = value

    model = create_model(args)
    state_dict = {re.sub(r"^model\.", "", k): v for k, v in ckpt["state_dict"].items()}
    model.load_state_dict(state_dict)
    
    return model.to(device)


class MolDynModel(nn.Module):
    def __init__(
        self,
        representation_model,
        output_model,
        prior_model=None,
        reduce_op="add",
        mean=None,
        std=None,
        derivative=False,
        cutoff=5.0,
        cutoff_pruning=1.6,
        max_num_neighbors=32,
        max_num_edges_save=32,
        many_body=True,
    ):
        super(MolDynModel, self).__init__()
        self.representation_model = representation_model

        if output_model is not None:
            self.output_model = output_model

        self.prior_model = prior_model
        # if not output_model.allow_prior_model and prior_model is not None:
            # self.prior_model = None
            # rank_zero_warn(
            #     "Prior model was given but the output model does "
            #     "not allow prior models. Dropping the prior model."
            # )

        self.reduce_op = reduce_op
        self.derivative = derivative

        mean = torch.scalar_tensor(0) if mean is None else mean
        self.register_buffer("mean", mean)
        std = torch.scalar_tensor(1) if std is None else std
        self.register_buffer("std", std)

        self.cutoff = cutoff
        self.cutoff_pruning = cutoff_pruning
        self.max_num_neighbors = max_num_neighbors
        self.max_num_edges_save = max_num_edges_save
        self.many_body = many_body

        self.reset_parameters()

    def reset_parameters(self):
        self.representation_model.reset_parameters()
        if isinstance(self.representation_model, MABNet):
            self.output_model.reset_parameters()
        if self.prior_model is not None:
            self.prior_model.reset_parameters()

    def forward(self, data: Data) -> Tuple[Tensor, Optional[Tensor]]:
        
        z, pos, batch = data.z, data.pos, data.batch
        edge_index = radius_graph(pos, r=self.cutoff, batch=batch, loop=True, max_num_neighbors=self.max_num_neighbors)

        if self.derivative:
            data.pos.requires_grad_(True)

        if isinstance(self.representation_model, MABNet):
            x, v = self.representation_model(data)  # [num_nodes, hidden_channels], [num_nodes, 8, hidden_channels]
            x = self.output_model.pre_reduce(x, v, data.z, data.pos, data.batch)    # [num_nodes, 1]

            x = x * self.std

            if self.prior_model is not None:
                x = self.prior_model(x, data.z)
            out = scatter(x, data.batch, dim=0, reduce=self.reduce_op)  # [num_nodes, 1] -> [batch_size, 1]
            out = self.output_model.post_reduce(out)    # [batch_size, 1]
            out = out + self.mean
        else:
            raise ValueError(f"Unknown model {self.representation_model}.")

        # compute gradients with respect to coordinates
        if self.derivative:
            grad_outputs: List[Optional[torch.Tensor]] = [torch.ones_like(out)]
            dy = grad(
                [out],
                [data.pos],
                grad_outputs=grad_outputs,
                create_graph=True,
                retain_graph=True,
            )[0]
            if dy is None:
                raise RuntimeError("Autograd returned None for the force prediction.")
            return out, -dy
        return out, None

