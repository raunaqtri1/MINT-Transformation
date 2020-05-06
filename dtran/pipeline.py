#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections import Counter
from pathlib import Path
from typing import *

from inspect import isgeneratorfunction
from marshmallow import Schema, fields, ValidationError
from networkx import DiGraph, lexicographical_topological_sort, bfs_edges, ancestors, NetworkXUnfeasible

from dtran.ifunc import IFunc
from dtran.wireio import WiredIOArg


class Pipeline(object):
    def __init__(self, func_classes: List[Type[IFunc]], wired: List[any] = None):
        """
        :param func_classes:
        :param wired: input, output
        """
        # map from function id to a tuple (idx of function, order of function (start from 1)).
        self.id2order = {}
        # map from idx of function to its order
        idx2order = {}
        # map from tuple (id, order) of function to its dataset preference
        self.preferences = {}
        # map from idx of generator function to its nesting level
        self.idx2level = {}
        # map from nesting level of generator function to its idx
        level2idx = {}

        for i, func_cls in enumerate(func_classes):
            if func_cls.id not in self.id2order:
                self.id2order[func_cls.id] = []
            self.id2order[func_cls.id].append((i, len(self.id2order[func_cls.id]) + 1))
            idx2order[i] = len(self.id2order[func_cls.id])
            self.preferences[(func_cls.id, idx2order[i])] = {}

        wired = wired or []
        for input, output in wired:
            if input[1] is None:
                input[1] = self.get_func_order(input[0])
            if output[1] is None:
                output[1] = self.get_func_order(output[0])

        self.wired = {}
        # applying topological sort on func_classes to determine execution order based on wiring
        graph = DiGraph()
        graph.add_nodes_from(range(len(func_classes)))
        # mapping preferences of argtype "dataset" to determine backend for "dataset" outputs
        preference_roots, preference_graph = [], DiGraph()
        for i, o in wired:
            input_arg = func_classes[self.id2order[i[0]][i[1] - 1][0]].inputs[i[2]]
            output_arg = func_classes[self.id2order[o[0]][o[1] - 1][0]].outputs[o[2]]
            if input_arg != output_arg:
                raise ValidationError(
                    f"Incompatible ArgType while wiring {WiredIOArg.get_arg_name(i[0], i[1], i[2])} to {WiredIOArg.get_arg_name(o[0], o[1], o[2])}")
            self.wired[WiredIOArg.get_arg_name(i[0], i[1], i[2])] = WiredIOArg.get_arg_name(o[0], o[1], o[2])
            graph.add_edge(self.id2order[o[0]][o[1] - 1][0], self.id2order[i[0]][i[1] - 1][0])

            if output_arg.id == 'dataset':
                self.preferences[(o[0], o[1])][o[2]] = None
                node = (o[0], o[1], 'o', o[2])
                # if input_ref of "dataset" output is None, we take it as a new "dataset"
                if output_arg.input_ref is None:
                    preference_roots.append(node)
                elif output_arg.input_ref not in func_classes[self.id2order[o[0]][o[1] - 1][0]].inputs:
                    raise ValidationError(
                        f"Invalid value for input_ref {output_arg.input_ref} of {WiredIOArg.get_arg_name(o[0], o[1], o[2])} output dataset")
                elif func_classes[self.id2order[o[0]][o[1] - 1][0]].inputs[output_arg.input_ref] != output_arg:
                    raise ValidationError(
                        f"Invalid ArgType for input_ref {output_arg.input_ref} of {WiredIOArg.get_arg_name(o[0], o[1], o[2])} output dataset")
                else:
                    # adding dummy "internal" edges within the same adapter to link "dataset" output to its input_ref
                    preference_graph.add_edge((o[0], o[1], 'i', output_arg.input_ref), node, preference='n/a')
                preference_graph.add_edge(node, (i[0], i[1], 'i', i[2]), preference=input_arg.preference)

        self.func_classes = []
        self.idx2order = {}
        self.level2idx = {}
        try:
            max_level = -1
            # reordering func_classes in topologically sorted order for execution
            for i in lexicographical_topological_sort(graph):
                self.func_classes.append(func_classes[i])
                # changing idx of functions to map to their new order
                self.idx2order[len(self.func_classes) - 1] = idx2order[i]
                level = 0
                for j in range(max_level, -1, -1):
                    if ancestors(graph, i) & level2idx[j]:
                        level = j + 1
                        break
                if isgeneratorfunction(func_classes[i].exec):
                    if level not in level2idx:
                        level2idx[level] = set()
                        self.level2idx[level] = set()
                    level2idx[level].add(i)
                    self.level2idx[level].add(len(self.func_classes) - 1)
                    max_level = max(max_level, level)
                self.idx2level[len(self.func_classes) - 1] = level
        except NetworkXUnfeasible:
            raise ValidationError("Pipeline is not a DAG")

        self.schema = {}
        for i, func_cls in enumerate(self.func_classes):
            for argname in func_cls.inputs:
                gname = WiredIOArg.get_arg_name(func_cls.id, self.idx2order[i], argname)
                if gname in self.wired:
                    continue
                argtype = func_cls.inputs[argname]
                self.schema[gname] = fields.Raw(required=not argtype.optional, validate=argtype.is_valid,
                                                error_messages={
                                                    'validator_failed': f"Invalid Argument type. Expected {argtype.id}"})
        self.schema = Schema.from_dict(self.schema)

        # setting preferences for new "dataset" outputs
        for root in preference_roots:
            counter = Counter()
            # traversing subgraph from every new "dataset" as root and counting preferences
            for edge in bfs_edges(preference_graph, root):
                counter[preference_graph[edge[0]][edge[1]]['preference']] += 1
            preference = None
            if counter['graph'] > counter['array']:
                preference = 'graph'
            elif counter['array'] > counter['graph']:
                preference = 'array'
            self.preferences[(root[0], root[1])][root[3]] = preference

    def exec(self, inputs: dict) -> dict:
        inputs_copy = {}
        for arg in inputs:
            if isinstance(arg, WiredIOArg):
                if arg.func_idx is None:
                    func_idx = self.get_func_order(arg.func_id)
                else:
                    func_idx = arg.func_idx
                inputs_copy[WiredIOArg.get_arg_name(arg.func_id, func_idx, arg.name)] = inputs[arg]
            else:
                inputs_copy[arg] = inputs[arg]
        inputs = inputs_copy
        self.validate(inputs)

        output = {}
        generators = {}
        generator_stack = []
        level = -1
        i = 0
        counter = {}
        while i < len(self.func_classes):
            gname = WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], next(iter(self.func_classes[i].outputs.keys())))
            if gname in output:
                if i == len(self.func_classes) - 1:
                    level, i = self.reset_generator_stack(generator_stack, level, generators, output, len(self.func_classes))
                    continue
                i += 1
                continue

            func_args = {}
            for argname in self.func_classes[i].inputs.keys():
                gname = WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)
                if gname in self.wired:
                    # wired has higher priority
                    func_args[argname] = output[self.wired[gname]]
                else:
                    try:
                        func_args[argname] = inputs[gname]
                    except KeyError as e:
                        if self.func_classes[i].inputs[argname].optional:
                            continue
                        raise e

            try:
                func = self.func_classes[i](**func_args)
                counter[i] = 1 if i not in counter else counter[i] + 1
            except TypeError:
                print(f"Cannot initialize cls: {self.func_classes[i]}")
                raise
            func.set_preferences(self.preferences[(self.func_classes[i].id, self.idx2order[i])])
            try:
                result = func.exec()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(e)
                level, i = self.reset_generator_stack(generator_stack, level, generators, output, i)
                continue

            if isgeneratorfunction(self.func_classes[i].exec):
                level = max(level, self.idx2level[i])
                if level == len(generator_stack):
                    generator_stack.append(i)
                generators[i] = result
                try:
                    result = next(generators[i])
                except StopIteration:
                    print(
                        f"Empty stream for {self.func_classes[i].id}__{self.idx2order[i]} with inputs {func_args}"
                    )
                    raise

            for argname in self.func_classes[i].outputs.keys():
                try:
                    output[WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)] = result[argname]
                except TypeError:
                    print(
                        f"Error while wiring output of {self.func_classes[i]} from {argname} to {WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)}"
                    )
                    raise

            if i == len(self.func_classes) - 1:
                level, i = self.reset_generator_stack(generator_stack, level, generators, output, len(self.func_classes))
                continue
            i += 1
        print(counter)
        return output

    def reset_generator_stack(self, generators_stack: List[int], level: int, generators: dict, output: dict, end: int) -> Tuple[int, int]:
        if level == -1:
            generators_stack.clear()
            generators.clear()
            output.clear()
            return level, len(self.func_classes)

        for i in self.level2idx[level]:
            try:
                result = next(generators[i])
            except StopIteration:
                generators_stack.pop()
                return self.reset_generator_stack(generators_stack, level - 1, generators, output, end)

            for argname in self.func_classes[i].outputs.keys():
                try:
                    output[WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)] = result[argname]
                except TypeError:
                    print(
                        f"Error while wiring output of {self.func_classes[i]} from {argname} to {WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)}"
                    )
                    raise

        for i in range(generators_stack[-1], end):
            if self.idx2level[i] <= level:
                continue
            if isgeneratorfunction(self.func_classes[i].exec):
                del generators[i]
            for argname in self.func_classes[i].outputs.keys():
                del output[WiredIOArg.get_arg_name(self.func_classes[i].id, self.idx2order[i], argname)]

        return level, generators_stack[-1]

    def validate(self, inputs: dict) -> None:
        errors = self.schema().validate(inputs)
        if errors:
            raise ValidationError(errors)

    def get_func_order(self, func_id: str) -> int:
        if len(self.id2order[func_id]) == 0:
            raise ValueError(f"Cannot wire argument to function {func_id} because it doesn't exist")

        if len(self.id2order[func_id]) > 1:
            raise ValueError(
                f"Cannot wire argument to function {func_id} because it is ambiguous (more than one function with same id)"
            )

        return self.id2order[func_id][0][-1]

    def save(self, save_path: Union[str, Path]):
        pass

    @staticmethod
    def load(load_path: Union[str, Path]):
        pass
