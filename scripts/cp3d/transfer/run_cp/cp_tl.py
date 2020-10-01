"""
A python wrapper around ChemProp that trains separate models using features
generated by different CP3D models.
"""

import os
import json
import argparse

from nff.utils import bash_command, parse_args

METRIC_CHOICES = ["auc",
                  "prc-auc",
                  "rmse",
                  "mae",
                  "mse",
                  "r2",
                  "accuracy",
                  "cross_entropy",
                  "single_class_entropy"]


def train(cp_folder,
          train_folder):

    train_script = os.path.join(cp_folder, "train.py")
    config_path = os.path.join(train_folder, "config.json")

    with open(config_path, "r") as f:
        config = json.load(f)

    data_path = config["data_path"]
    dataset_type = config["dataset_type"]
    cmd = (f"python {train_script} --config_path {config_path} "
           f" --data_path {data_path} "
           f" --dataset_type {dataset_type}")

    p = bash_command(cmd)
    p.communicate()


def modify_config(base_config_path,
                  metric,
                  train_feat_path,
                  val_feat_path,
                  test_feat_path,
                  train_folder,
                  features_only):

    with open(base_config_path, "r") as f:
        config = json.load(f)

    dic = {"metric": metric,
           "features_path": [train_feat_path],
           "separate_val_features_path": [val_feat_path],
           "separate_test_features_path": [test_feat_path],
           "save_dir": train_folder,
           "features_only": features_only}

    config.update({key: val for key, val in
                   dic.items() if val is not None})

    new_config_path = os.path.join(train_folder, "config.json")
    if not os.path.isdir(train_folder):
        os.makedirs(train_folder)

    with open(new_config_path, "w") as f:
        json.dump(config, f, indent=4, sort_keys=True)


def main(base_config_path,
         train_folder,
         metric,
         train_feat_path,
         val_feat_path,
         test_feat_path,
         cp_folder,
         features_only,
         **kwargs):

    modify_config(base_config_path=base_config_path,
                  metric=metric,
                  train_feat_path=train_feat_path,
                  val_feat_path=val_feat_path,
                  test_feat_path=test_feat_path,
                  train_folder=train_folder,
                  features_only=features_only)

    train(cp_folder=cp_folder,
          train_folder=train_folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base_config_path", type=str,
                        help=("Path to the reference config file "
                              "used to train a ChemProp model. "
                              "This file will be modified with "
                              "the arguments specified below "
                              "(metric and features paths)."
                              "If they are not specified then "
                              "the config file will not be "
                              "modified."))

    parser.add_argument("--metric", type=str,
                        choices=METRIC_CHOICES,
                        help=("Metric for which to evaluate "
                              "the model performance"),
                        default=None)
    parser.add_argument("--train_feat_path", type=str,
                        help=("Path to features file for training set"),
                        default=None)
    parser.add_argument("--val_feat_path", type=str,
                        help=("Path to features file for validation set"),
                        default=None)
    parser.add_argument("--test_feat_path", type=str,
                        help=("Path to features file for test set"),
                        default=None)
    parser.add_argument("--train_folder", type=str,
                        help=("Folder in which you will store the "
                              "ChemProp model."),
                        default=None)
    parser.add_argument("--features_only", action='store_true',
                        help=("Train model with only the stored features"))
    parser.add_argument("--cp_folder", type=str,
                        help=("Path to ChemProp folder."))
    parser.add_argument('--this_config_file', type=str,
                        help=("Path to JSON file with arguments "
                              "for this script. If given, any "
                              "arguments in the file override the "
                              "command line arguments."))

    args = parse_args(parser, config_flag="this_config_file")
    main(**args.__dict__)
