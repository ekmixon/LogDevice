#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import difflib
import json
import os
import subprocess
import tempfile
import time
import typing
from textwrap import dedent

import nubia
import pygments
import termcolor
from ldshell.helpers import ask_prompt, confirm_prompt
from logdevice.admin.cluster_membership.types import (
    BootstrapClusterRequest,
    RemoveNodesRequest,
)
from logdevice.admin.nodes.types import NodesFilter
from logdevice.common.types import LocationScope, NodeID, ReplicationProperty
from logdevice.ops import nodes_configuration_manager as ncm
from pygments import formatters, lexers
from thrift.py3 import RpcOptions


class EditorError(Exception):
    pass


class NodesConfigError(Exception):
    pass


class NoChangesError(Exception):
    pass


class NCMError(Exception):
    pass


def _get_client():
    return nubia.context.get_context().build_client(
        settings={
            # Read NCM from Zookeeper direectly instead of other nodes.
            "admin-client-capabilities": "true"
        }
    )


def _get_nodes_config(client):
    ncm_bin = ncm.get_nodes_configuration(client)
    ncm_json_str = ncm.nodes_configuration_to_json(ncm_bin)
    return json.loads(ncm_json_str)


def _edit_text_with_editor(text: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json") as temp_file:
        with open(temp_file.name, "w") as tmpf:
            tmpf.write(text)
            tmpf.flush()

        editor = os.environ.get("EDITOR", "nano")
        pr = subprocess.Popen([editor, temp_file.name])
        pr.wait()
        if pr.returncode != 0:
            raise EditorError("Non-zero return code from editor")

        with open(temp_file.name, "r") as tmpf:
            new_text = tmpf.read()

    return new_text


@nubia.command("nodes-config")
class NodesConfig:
    """Manipulates the cluster's NodesConfig for NodesConfigurationManager
    enabled clusters.
    """

    @nubia.command
    async def show(self):
        """Print tier's NodesConfig to stdout"""

        try:
            client = _get_client()
            nc = _get_nodes_config(client)
        except Exception as e:
            termcolor.cprint(str(e), "red")
            return 1

        print(
            pygments.highlight(
                json.dumps(nc, indent=4, sort_keys=True),
                lexers.JsonLexer(),
                formatters.TerminalFormatter(),
            )
        )
        return 0

    @nubia.command
    async def edit(self):
        """Open the tier's NodesConfig in a text editor. Will try to use $EDITOR
        environment variable. If not set it falls back to `nano`
        """

        try:
            client = _get_client()
            nc = _get_nodes_config(client)
        except Exception as e:
            termcolor.cprint(str(e), "red")
            return 1

        formatted = json.dumps(nc, indent=4, sort_keys=True)

        edited_text = None
        while True:
            try:
                edited_text = _edit_text_with_editor(
                    formatted if edited_text is None else edited_text
                )
                edited_nc = json.loads(edited_text)
                nc_list = json.dumps(nc, indent=4, sort_keys=True).split("\n")
                edited_nc_list = json.dumps(edited_nc, indent=4, sort_keys=True).split(
                    "\n"
                )
                diff = difflib.unified_diff(nc_list, edited_nc_list, lineterm="")

                if next(diff, None) is None:
                    raise EditorError("No changes detected")

                if edited_nc.get("version", nc["version"]) == nc["version"]:
                    edited_nc["version"] = nc["version"] + 1

                if (
                    edited_nc.get("last_timestamp", nc["last_timestamp"])
                    == nc["last_timestamp"]
                ):
                    edited_nc["last_timestamp"] = int(time.time() * 1000)  # time in ms

                # It seems everything is fine, it's time to show final diff
                edited_nc_json_str = json.dumps(edited_nc, indent=4, sort_keys=True)
                edited_nc_list = edited_nc_json_str.split("\n")
                diff = difflib.unified_diff(nc_list, edited_nc_list, lineterm="")
                termcolor.cprint("You're going to apply the following diff:", "red")
                for line in diff:
                    print(line)

                termcolor.cprint("\nWhat to do now?")
                termcolor.cprint("[1] Apply")
                termcolor.cprint("[2] Edit")
                termcolor.cprint("[3] Cancel")
                choice = ask_prompt("Choice:", options=("1", "2", "3"))
                if choice == "2":
                    continue
                elif choice == "3":
                    break

                # Need to catch all exceptions from NCM and re-raise it
                # to distinguish from other errors
                try:
                    nc_bin = ncm.json_to_nodes_configuration(edited_nc_json_str)
                except Exception as e:
                    raise NCMError(f"Error on serializing config: {e}")

                try:
                    ncm.overwrite_nodes_configuration(client, nc_bin)
                    break
                except Exception as e:
                    raise NCMError(f"Error on overwriting config: {e}")

            except (EditorError, json.JSONDecodeError, NCMError) as e:
                termcolor.cprint(str(e), "red")
                if not confirm_prompt("Try again?"):
                    break

                continue

        return 0

    @nubia.command
    # pyre-fixme[56]: Argument `textwrap.dedent("
    @nubia.argument(
        "metadata_replicate_across",
        type=typing.Mapping[str, int],
        description=dedent(
            """
            Defines cross-domain replication for metadata logs. A vector of replication factors
            at various scopes. When this option is given, replicationFactor_ is
            optional. This option is best explained by examples:
                - "node: 3, rack: 2" means "replicate each record to at least 3 nodes
                in at least 2 different racks".
                - "rack: 2" with replicationFactor_ = 3 mean the same thing.
                - "rack: 3, region: 2" with replicationFactor_ = 4 mean "replicate
                each record to at least 4 nodes in at least 3 different racks in at
                least 2 different regions"
                - "rack: 3" means "replicate each record to at least 3 nodes in
                at least 3 different racks".
                - "rack: 3" with replicationFactor_ = 3 means the same thing.
             """
        ),
    )
    async def bootstrap(self, metadata_replicate_across: typing.Mapping[str, int]):
        """
        In a newly bootstrapped cluster, this call is needed to finalize the
        bootstrapping and allow the cluster to be used. This call will:
        1- Enable sequencing on all the nodes that have sequencer role.
        2- Transition storage nodes that are done provisioning into ReadWrite.
        3- Create the initial metadata nodeset according to the passed
            replication property.
        4- Mark the NodesConfiguration as bootstrapped.

        Calls to this function will fail if the cluster is already bootstrapped
        or if there are not enough provisioned storage nodes to satisfy the
        requested replication property.
        """

        # pyre-fixme[9]: replication_property has type `Map__LocationScope_i32`;
        #  used as `Dict[Variable[_KT], Variable[_VT]]`.
        replication_property: ReplicationProperty = {}
        for scope_str, factor in metadata_replicate_across.items():
            try:
                scope = LocationScope[scope_str.upper()]
                # pyre-fixme[16]: `Map__LocationScope_i32` has no attribute
                #  `__setitem__`.
                replication_property[scope] = factor
            except KeyError:
                termcolor.cprint(f"{scope_str} is not a valid scope", "red")
                return 1

        ctx = nubia.context.get_context()
        async with ctx.get_cluster_admin_client() as client:
            try:
                await client.bootstrapCluster(
                    request=BootstrapClusterRequest(
                        metadata_replication_property=replication_property
                    ),
                    # pyre-fixme[28]: Unexpected keyword argument `timeout`.
                    rpc_options=RpcOptions(timeout=60),
                )
                termcolor.cprint("Successfully bootstrapped the cluster", "green")
            except Exception as e:
                termcolor.cprint(str(e), "red")
                return 1

    @nubia.command
    @nubia.argument("node_indexes", description="Apply maintenance to specified nodes")
    async def shrink(self, node_indexes: typing.List[int]):
        """
        Shrinks the cluster by removing nodes from the NodesConfig. This
        operation requires that the removed nodes are empty (storage state:
        NONE) and dead.
        """

        ctx = nubia.context.get_context()
        async with ctx.get_cluster_admin_client() as client:
            try:
                await client.removeNodes(
                    request=RemoveNodesRequest(
                        node_filters=[
                            NodesFilter(node=NodeID(node_index=idx))
                            for idx in node_indexes
                        ]
                    ),
                    # pyre-fixme[28]: Unexpected keyword argument `timeout`.
                    rpc_options=RpcOptions(timeout=60),
                )
                termcolor.cprint("Successfully removed the nodes", "green")
            except Exception as e:
                termcolor.cprint(str(e), "red")
                return 1
