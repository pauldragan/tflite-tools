import sys
import csv
from collections import namedtuple
import functools
from pathlib import Path

from .tflite import Model
from .tflite.BuiltinOperator import BuiltinOperator
from .tflite.TensorType import TensorType
from flatbuffers.number_types import UOffsetTFlags
import tensorflow.lite as tf_lite
import numpy as np
from tqdm import tqdm
from prettytable import PrettyTable


def cluster_weights(weights, n_clusters):
    from sklearn import cluster
    kmeans = cluster.KMeans(n_clusters=n_clusters).fit(weights.reshape((-1, 1)))
    return kmeans.labels_.reshape(weights.shape), np.around(kmeans.cluster_centers_).astype(np.int32)


# Flatbuffers provide a per-byte view on data, so we need to cast the underlying buffer to the correct datatype
def get_buffer_as_numpy(tensor, buffer):
    if tensor.Type() == TensorType.UINT8:
        arr = buffer.DataAsNumpy()
    elif tensor.Type() == TensorType.INT16:
        arr = np.frombuffer(buffer.DataAsNumpy(), dtype=np.dtype(np.int16).newbyteorder("<"))
    elif tensor.Type() == TensorType.INT32:
        arr = np.frombuffer(buffer.DataAsNumpy(), dtype=np.dtype(np.int32).newbyteorder("<"))
    elif tensor.Type() == TensorType.INT64:
        arr = np.frombuffer(buffer.DataAsNumpy(), dtype=np.dtype(np.int64).newbyteorder("<"))
    else:
        raise NotImplementedError()
    return arr.reshape(tensor.ShapeAsNumpy())


def get_buffer_element_size(t):
    sizes = {
        TensorType.UINT8: 1,
        TensorType.INT16: 2,
        TensorType.INT32: 4,
        TensorType.INT64: 8,
        TensorType.FLOAT32: 4,
        TensorType.FLOAT16: 2,
    }
    return sizes[t]


class TFLiteTensor:
    def __init__(self, id=None, shape=None, name=None, is_constant=False, producer=None,
                 consumers=None, predecessors=None, type=None):
        self.id = id
        self.shape = shape
        self.name = name
        self.is_constant = is_constant
        self.producer = producer
        self.consumers = consumers if consumers is not None else []
        self.predecessors = predecessors
        self.type = type

    @property
    def size(self):
        return 0 if self.is_constant else np.prod(self.shape) * get_buffer_element_size(self.type)

    def __hash__(self):
        return hash(self.id)


class TFLiteOperator:
    def __init__(self, id=None, output=None, inputs=None, opcode=None, fixed_to=()):
        self.id = id
        self.output = output
        self.inputs = inputs if inputs is not None else []
        self.opcode = opcode
        self.fixed_to = fixed_to

    def __hash__(self):
        return hash(self.id)


TFLiteGraph = namedtuple("TFLiteGraph", ["tensors", "operators", "inputs", "outputs"])


class TFLiteModel:
    def __init__(self, model_bytes):
        self.model_bytes = model_bytes
        self.model_graph = None
        self.peak_usage = None

    @classmethod
    def create_from_protobuf(cls, protobuf_file, inputs, outputs, input_shapes):
        converter = tf_lite.TFLiteConverter.from_frozen_graph(protobuf_file, input_arrays=inputs,
                                                              output_arrays=outputs, input_shapes=input_shapes)
        from tensorflow.lite.python import lite_constants
        converter.inference_type = lite_constants.QUANTIZED_UINT8
        converter.inference_input_type = lite_constants.QUANTIZED_UINT8
        # converter.optimizations = [tf_lite.Optimize.DEFAULT]
        input_arrays = converter.get_input_arrays()
        converter.quantized_input_stats = {input_arrays[0]: (0, 1)}  # mean, std_dev
        return cls(bytearray(converter.convert()))

    @classmethod
    def load_from_file(cls, model_path):
        with open(model_path, 'rb') as f:
            return cls(bytearray(f.read()))

    def write_to_file(self, output_path):
        with open(output_path, "wb") as f:
            f.write(self.model_bytes)

    def cluster_weights(self, weight_clusters):
        print(f"Clustering weights into {weight_clusters} clusters...")
        weights = self._discover_tflite_weights()
        for b_index, weight in weights:
            assignments, centroids = cluster_weights(weight, weight_clusters)
            self._overwrite_flatbuffers_buffer(b_index, np.squeeze(centroids[assignments], axis=-1))

    def _overwrite_flatbuffers_buffer(self, buffer_idx, new_contents):
        model = Model.Model.GetRootAsModel(self.model_bytes, 0)
        orig_buffer = model.Buffers(buffer_idx)
        # NB. Update this to directly manipulate `serialized_model` if this view becomes unwriteable
        orig_buffer.DataAsNumpy()[:] = new_contents.astype(np.uint8).flatten()

    def _discover_tflite_weights(self):
        model = Model.Model.GetRootAsModel(self.model_bytes, 0)
        subgraph = model.Subgraphs(0)

        weights = []
        for o in range(subgraph.OperatorsLength()):
            op = subgraph.Operators(o)
            opcode = model.OperatorCodes(op.OpcodeIndex()).BuiltinCode()
            inputs = op.InputsAsNumpy()

            parametrised_opcodes = [BuiltinOperator.CONV_2D, BuiltinOperator.FULLY_CONNECTED, BuiltinOperator.DEPTHWISE_CONV_2D]
            if opcode not in parametrised_opcodes:
                continue

            weight_tensor = subgraph.Tensors(inputs[1])
            buffer_idx = weight_tensor.Buffer()
            buffer = model.Buffers(buffer_idx)
            # Return a buffer index and contents as an ndarray
            weights.append((buffer_idx, get_buffer_as_numpy(weight_tensor, buffer)))

        return weights

    def _build_graph(self):
        model = Model.Model.GetRootAsModel(self.model_bytes, 0)
        subgraph = model.Subgraphs(0)

        tensors = []
        operators = []

        for i in range(subgraph.TensorsLength()):
            t = subgraph.Tensors(i)
            tensors.append(TFLiteTensor(id=i, shape=t.ShapeAsNumpy(), name=t.Name().decode("ascii"),
                                        producer=None, consumers=[], type=t.Type()))

        for i in range(subgraph.OperatorsLength()):
            op = subgraph.Operators(i)
            assert op.OutputsLength() <= 1
            has_output = op.OutputsLength() == 1
            inputs = [tensors[j] for j in op.InputsAsNumpy()]
            assert len(inputs) > 0

            opcode = model.OperatorCodes(op.OpcodeIndex()).BuiltinCode()
            tflite_op = TFLiteOperator(id=i,
                                       opcode=opcode,
                                       output=tensors[op.Outputs(0)] if has_output else None,
                                       inputs=inputs)
            tflite_op.output.producer = tflite_op

            if opcode == BuiltinOperator.ADD:
                in0 = inputs[0]
                for t in inputs[1:]:
                    tprod = t.producer
                    tprod.fixed_to = [in0.id]
                tflite_op.fixed_to = [in0.id]
            elif (opcode == BuiltinOperator.MAX_POOL_2D or
                  opcode == BuiltinOperator.AVERAGE_POOL_2D):
                in0 = inputs[0]
                tflite_op.fixed_to = [in0.id]
            elif opcode == BuiltinOperator.CONCATENATION:
                fixed_to = []
                for t in inputs:
                    tprod = t.producer
                    fixed_to.append(t.id)
                tflite_op.fixed_to = fixed_to
            elif opcode == BuiltinOperator.RESIZE_BILINEAR:
                in0 = inputs[0]
                tflite_op.fixed_to = [in0.id]

            if opcode != BuiltinOperator.CONV_2D:
                print("Must fix operator!", opcode, " fixed to: ", tflite_op.fixed_to)

            for t in inputs:
                t.consumers.append(tflite_op)
            operators.append(tflite_op)

        inputs = [tensors[j] for j in subgraph.InputsAsNumpy()]
        outputs = [tensors[j] for j in subgraph.OutputsAsNumpy()]

        for t in tensors:
            t.is_constant = (t.producer is None) and (t not in inputs)

        # Can turn into an iterative function if this ever causes performance / stack overflow issues
        def _compute_predecessors(tensor):
            if tensor.predecessors is not None:
                return tensor.predecessors

            if tensor.producer is None:
                tensor.predecessors = set()
            else:
                op_inputs = tensor.producer.inputs
                tensor.predecessors = set(op_inputs)
                for i in op_inputs:
                    tensor.predecessors |= _compute_predecessors(i)
            return tensor.predecessors

        for o in outputs:
            _compute_predecessors(o)  # Will recursively compute predecessors for all nodes leading up to output nodes

        self.model_graph = TFLiteGraph(tensors, operators, inputs, outputs)

    @staticmethod
    def _cum_tensor_sizes(tensors):
        return sum(t.size for t in tensors)

    def peak_mem_usage(self):
        if self.peak_usage is not None:
            return self.peak_usage

        if not self.model_graph:
            self._build_graph()
        g = self.model_graph

        # Can turn into an iterative function if this ever causes performance / stack overflow issues
        @functools.lru_cache(maxsize=None)
        def mem(tensors):
            # Computes the peak memory usage of a runtime system that computes all tensors in a set `tensors`.
            constants = [t for t in tensors if t.producer is None]
            if constants:
                upstream_mem_use, op_order = mem(frozenset(t for t in tensors if t.producer is not None))
                return TFLiteModel._cum_tensor_sizes(constants) + upstream_mem_use, op_order
            if not tensors:
                return 0, []

            min_use = sys.maxsize  # A reasonably large integer
            op_order = []
            # For each of tensors in our working set, we try to unapply the operator that produced it
            for t in tensors:
                rest = tensors - {t}
                # We constrain the search to never consider evaluating an operator (`t.producer`) more than once ---
                # so we prevent cases where we consider unapplying `t.producer` but it's actually necessary for other
                # tensors in the working set.
                if any(t in r.predecessors for r in rest):
                    continue
                inputs = frozenset(t.producer.inputs)
                new_set = rest | inputs
                upstream_mem_use, operators = mem(new_set)

                tensors_in_memory = new_set | {t}
                mem_use = max(upstream_mem_use, TFLiteModel._cum_tensor_sizes(tensors_in_memory))
                if mem_use < min_use:
                    min_use = mem_use
                    op_order = operators + [t.producer]
            return min_use, op_order

        self.peak_usage = mem(frozenset(g.outputs))
        return self.peak_usage

    def evaluate(self, test_data):
        interpreter = tf_lite.Interpreter(model_content=bytes(self.model_bytes))
        interpreter.allocate_tensors()
        input_info = interpreter.get_input_details()[0]
        input_index = input_info["index"]
        scale, offset = input_info["quantization"]
        output_index = interpreter.get_output_details()[0]["index"]

        total, correct = 0, 0
        for img, label in tqdm(test_data):
            # TODO: determine the required input type from the model
            interpreter.set_tensor(input_index, np.expand_dims((img / scale + offset).astype(np.uint8), axis=0))
            interpreter.invoke()
            predictions = interpreter.get_tensor(output_index)
            if len(label.shape) > 0:
                label = label.argmax()
            if predictions.argmax() == label:
                correct += 1
            total += 1
        print(f"{correct} classified correctly out of {total} ({correct / total * 100:.2f}%)")

    def _execution_schedule_info(self):
        if not self.model_graph:
            self._build_graph()
        g = self.model_graph

        # Compute tensor lifetimes
        num_operators = len(g.operators)
        first_used_at = {t: t.producer.id if t.producer is not None else 0 for t in g.tensors}
        last_used_at = {t: max(op.id for op in t.consumers) if t.consumers else num_operators for t in g.tensors}

        schedule = []
        for op in g.operators:
            tensors = {t for t in g.tensors if first_used_at[t] <= op.id <= last_used_at[t]}
            mem_use = TFLiteModel._cum_tensor_sizes(tensors)
            schedule.append((op, tensors, mem_use))

        return schedule

    def _shorten_long_name(self, name, max_characters=80):
        assert max_characters >= 4
        if len(name) > max_characters:
            name_chars = max_characters - 3  # for ellipsis
            left = name_chars // 2
            right = name_chars - left
            return name[:left] + "..." + name[-right:]
        else:
            return name

    def _print_execution_schedule(self):
        x = PrettyTable()
        x.field_names = ["Operator (output name)", "Tensors in memory (IDs)", "Memory use (B)"]
        x.align["Memory use (B)"] = "r"

        schedule = self._execution_schedule_info()
        peak_mem_use = 0
        for item in schedule:
            op, working_set, mem_use = item
            peak_mem_use = max(peak_mem_use, mem_use)
            name = self._shorten_long_name(op.output.name)
            x.add_row([name, f"[{', '.join(str(t.id) for t in working_set if t.size != 0)}]", f"{mem_use:,}"])

        print("Operator execution schedule:")
        print(x)
        print(f"Current peak memory usage: {peak_mem_use:,} B")
        print()

    def _output_execution_schedule_to_csv(self, csv_file):
        with open(csv_file, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(["Operator", "Working set", "Memory use"])

            schedule = self._execution_schedule_info()
            for item in schedule:
                op, working_set, mem_use = item
                w.writerow([op.output.name, ' '.join(str(t.id) for t in working_set if t.size != 0), mem_use])

    def _print_tensor_details(self):
        if not self.model_graph:
            self._build_graph()

        x = PrettyTable()
        x.field_names = ["Id", "Tensor", "Shape", "Size in RAM (B)"]
        x.align["Id"] = "r"
        x.align["Size in RAM (B)"] = "r"

        for t in self.model_graph.tensors:
            if t.size != 0:
                x.add_row([t.id, self._shorten_long_name(t.name), tuple(t.shape), f"{t.size:,}"])

        print("Tensor information (weights excluded):")
        print(x)
        print()

    def plot_memory_usage(self, plot_file):
        """
        Plots memory usage for each operator in the schedule as a stacked bar chart.
        :param plot_file: Output file
        """
        import matplotlib.pyplot as plt

        labels = []
        input_sizes = []
        output_sizes = []
        other_sizes = []

        schedule = self._execution_schedule_info()
        peak_mem_use = 0

        for item in schedule:
            op, working_set, mem_use = item

            input_size = TFLiteModel._cum_tensor_sizes(op.inputs)
            output_size = op.output.size
            other_size = TFLiteModel._cum_tensor_sizes(t for t in working_set if t not in op.inputs and t != op.output)

            assert input_size + output_size + other_size == mem_use
            peak_mem_use = max(peak_mem_use, mem_use)

            labels.append(op.output.name)
            input_sizes.append(input_size)
            output_sizes.append(output_size)
            other_sizes.append(other_size)

        input_sizes = np.array(input_sizes) / 1024
        output_sizes = np.array(output_sizes) / 1024
        other_sizes = np.array(other_sizes) / 1024
        peak_mem_use /= 1024

        fig = plt.figure(figsize=(max(len(labels) / 3.5, 6), 8))
        fig.tight_layout()
        ax = fig.gca()
        x = np.arange(0, len(labels))

        ax.bar(x, input_sizes, color="#D95319", label="Operator inputs")
        ax.bar(x, output_sizes, bottom=input_sizes, color="#EDB120", label="Operator outputs")
        ax.bar(x, other_sizes, bottom=(input_sizes + output_sizes), color="#0072BD", label="Other tensors")

        ax.set_xticks(x)
        ax.set_xlabel('Operators')
        ax.set_ylabel('Memory usage (KB)')
        ax.set_ylim([0, peak_mem_use + 10])
        ax.set_xticklabels(labels, rotation=90)
        ax.legend()

        plt.savefig(plot_file, bbox_inches='tight', dpi=300)

    def _output_tensor_details_to_csv(self, csv_file):
        if not self.model_graph:
            self._build_graph()

        with open(csv_file, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(["Id", "Name", "Shape", "Size", "FixedTo"])

            for t in self.model_graph.tensors:
                if t.size != 0:
                    if t.producer is not None:
                        fixed_to = " ".join(str(i) for i in t.producer.fixed_to)
                    else:
                        fixed_to = None
                    w.writerow([t.id, t.name, ' '.join(str(i) for i in t.shape), t.size, fixed_to])

    def print_model_analysis(self):
        self._print_tensor_details()
        self._print_execution_schedule()

    def output_model_analysis_to_csv(self, output_folder):
        output_folder = Path(output_folder)
        assert output_folder.is_dir()
        self._output_tensor_details_to_csv(output_folder / "tensor_details.csv")
        self._output_execution_schedule_to_csv(output_folder / "execution_schedule_info.csv")

    def optimize_memory(self):
        _, op_order = self.peak_mem_usage()
        num_operators = len(self.model_graph.operators)
        correctly_ordered = all(i == op_order[i].id for i in range(num_operators))
        if correctly_ordered:
            print("The model already has optimal operator order.")
            return

        # Proceed reordering the operators by changing the indirection table
        model = Model.Model.GetRootAsModel(self.model_bytes, 0)
        subgraph = model.Subgraphs(0)
        indirection_table_offset = UOffsetTFlags.py_type(subgraph._tab.Offset(10))
        indirection_table = subgraph._tab.GetVectorAsNumpy(UOffsetTFlags, indirection_table_offset)
        old_indirection_table = indirection_table.copy()

        for i in range(num_operators):
            # Operator #op_id should go into position i
            op_id = op_order[i].id
            indirection_table[i] = old_indirection_table[op_id] + 4 * (op_id - i)
            op_order[i].id = i

        # Patch up model_graph instead of rebuilding it
        self.model_graph.operators.sort(key=lambda op: op.id)
