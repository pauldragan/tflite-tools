import argparse
import os

from tflite_tools import TFLiteModel


def main():
    parser = argparse.ArgumentParser(description='TFLite model analyser & memory optimizer')

    parser.add_argument("-i", type=str, dest="input_path", help="input model file (.tflite)")
    parser.add_argument("-o", type=str, dest="output_path", default=None, help="output model file (.tflite)")
    parser.add_argument("--clusters", type=int, default=0,
                        help="cluster weights into n-many values (simulate code-book quantization)")
    parser.add_argument("--optimize", action="store_true", default=False, help="optimize peak working set size")
    parser.add_argument("--csv", type=str, dest="csv_output_folder", default=None,
                        help="output model analysis in CSV format into the specified folder")
    parser.add_argument("--plot", type=str, dest="plot_file", default=None,
                        help="plot memory usage for each operator during the execution")
    args = parser.parse_args()

    # Example API usage:
    # Can also use `TFLiteModel.create_from_protobuf`, which will invoke TOCO.

    model = TFLiteModel.load_from_file(args.input_path)

    if args.optimize:
        print("Optimizing peak memory usage...")
        model.optimize_memory()

    if args.csv_output_folder:
        print(f"Writing model analysis to {args.csv_output_folder} in CSV format")
        os.makedirs(args.csv_output_folder, exist_ok=True)
        model.output_model_analysis_to_csv(args.csv_output_folder)
    else:
        model.print_model_analysis()

    if args.clusters > 0:
        model.cluster_weights(args.clusters)

    if args.plot_file:
        print(f"Plotting operator memory usage to {args.plot_file}")
        model.plot_memory_usage(args.plot_file)

    # Example API usage:
    # `model.evaluate(<data_iterator>)`
    # where data iterator returns `img`, `label`.
    # `img` will be cast to uint8 and `label` is assumed to be a one-hot vector

    if args.output_path:
        print(f"Saving the model to {args.output_path}...")
        model.write_to_file(args.output_path)


if __name__ == "__main__":
    main()
