#!/usr/bin/env python3
import logging
import json
import os

from sklearn.ensemble import RandomForestRegressor
from sklearn.feature_selection import SelectFromModel

from p1204_3.utils import assert_file
from p1204_3.utils import assert_msg
from p1204_3.utils import ffprobe
from p1204_3.modelutils import map_to_45
from p1204_3.modelutils import map_to_5
from p1204_3.modelutils import r_from_mos
from p1204_3.modelutils import mos_from_r
from p1204_3.modelutils import load_serialized
from p1204_3.modelutils import binarize_column
from p1204_3.modelutils import load_dict_values
from p1204_3.modelutils import per_sample_interval_function
from p1204_3.generic import *
from p1204_3.videoparser import *

import p1204_3.features  as features
from p1204_3.features import *



class P1204BitstreamMode3:
    def __init__(self):
        self.display_res = 3840*2160

    def _calculate(self, prediction_features, params, rf_model, display_res, device_type):
        def mos_q_baseline_pc(features, a, b, c, d):
            quant = features["quant"]
            mos_q = a + b * np.exp(c * quant + d)
            mos_q = np.clip(mos_q,1,5)
            mos_q = np.vectorize(r_from_mos)(mos_q)
            cod_deg = 100 - mos_q
            cod_deg = np.clip(cod_deg,0,100)
            return cod_deg

        prediction_features = prediction_features.copy()

        prediction_features = load_dict_values(prediction_features, "QPValuesStatsPerGop")
        prediction_features = load_dict_values(prediction_features, "QPstatspersecond")
        prediction_features = load_dict_values(prediction_features, "BitstreamStatFeatures")
        prediction_features = load_dict_values(prediction_features, "FramesizeStatsPerGop")
        prediction_features = load_dict_values(prediction_features, "AvMotionStatsPerGop")

        prediction_features["quant"] = prediction_features["QPValuesStatsPerGop_mean_Av_QPBB_non-i"]

        def video_codec_extension(row):
            if row["Codec"] == "h264" and row["BitDepth"] == 10:
                return "h264_10bit"
            if row["Codec"] == "hevc" and row["BitDepth"] == 10:
                return "hevc_10bit"
            return row["Codec"]

        prediction_features["video_codec"] = prediction_features.apply(video_codec_extension, axis=1)

        def norm_qp(row):
            if row["video_codec"] == "h264":
                return row["QPValuesStatsPerGop_mean_Av_QPBB_non-i"] / 51
            if row["video_codec"] == "h264_10bit":
                return row["QPValuesStatsPerGop_mean_Av_QPBB_non-i"] / 63
            if row["video_codec"] == "hevc":
                return row["QPValuesStatsPerGop_mean_Av_QPBB_non-i"] / 51
            if row["video_codec"] == "hevc_10bit":
                return row["QPValuesStatsPerGop_mean_Av_QPBB_non-i"] / 63
            if row["video_codec"] == "vp9":
                return row["QPValuesStatsPerGop_mean_Av_QPBB_non-i"] / 255
            return -1

        prediction_features["quant"] = prediction_features.apply(norm_qp, axis=1)

        prediction_features = binarize_column(prediction_features, "video_codec")

        codecs = prediction_features["video_codec"].unique()

        cod_deg = sum([prediction_features[c] * mos_q_baseline_pc(prediction_features, params[c + "_a"], params[c + "_b"],
                                                    params[c + "_c"], params[c + "_d"]) for c in codecs])


        resolution = params["x"] * np.log(params["y"] * (prediction_features["Resolution"]/display_res))
        resolution = np.clip(resolution, 0, 100)

        framerate = params["z"] * np.log(params["k"] * prediction_features["Framerate"]/60)
        framerate = np.clip(framerate, 0, 100)

        pred = 100 - (cod_deg + resolution + framerate)
        pred = np.vectorize(mos_from_r)(pred)
        pred = np.clip(pred, 1, 5)
        initial_predicted_score = np.vectorize(map_to_5)(pred)
        prediction_features["predicted_mos_mode3_baseline"] = initial_predicted_score

        residual_rf_model = load_serialized(rf_model)

        prediction_features_rf = prediction_features.copy()
        prediction_features_rf["h264"] = 0
        prediction_features_rf["hevc"] = 0
        prediction_features_rf["vp9"] = 0
        prediction_features_rf["h264_10bit"] = 0
        prediction_features_rf["hevc_10bit"] = 0

        def fill_codec(row):
            if row["video_codec"] == "h264":
                row["h264"] = 1
                row["hevc"] = 0
                row["vp9"] = 0
                row["h264_10bit"] = 0
                row["hevc_10bit"] = 0
                return row  # row["h264"]
            if row["video_codec"] == "h264_10bit":
                row["h264"] = 0
                row["hevc"] = 0
                row["vp9"] = 0
                row["h264_10bit"] = 1
                row["hevc_10bit"] = 0
                return row  # row["h264"]
            if row["video_codec"] == "hevc":
                row["h264"] = 0
                row["hevc"] = 1
                row["vp9"] = 0
                row["h264_10bit"] = 0
                row["hevc_10bit"] = 0
                return row  # row["hevc"]
            if row["video_codec"] == "hevc_10bit":
                row["h264"] = 0
                row["hevc"] = 0
                row["vp9"] = 0
                row["h264_10bit"] = 0
                row["hevc_10bit"] = 1
                return row  # row["h264"]
            if row["video_codec"] == "vp9":
                row["h264"] = 0
                row["hevc"] = 0
                row["vp9"] = 1
                row["h264_10bit"] = 0
                row["hevc_10bit"] = 0
                return row  # row["vp9"]
            return -1

        prediction_features_rf = prediction_features_rf.apply(fill_codec, axis=1)

        prediction_features_rf = prediction_features_rf.rename(columns={"FramesizeStatsPerGop_1.0_quantil_FrameSize": "1.0_quantil_FrameSize",
                                                                        "FramesizeStatsPerGop_std_FrameSize_non-i":"std_FrameSize_non-i",
                                                                        "FramesizeStatsPerGop_kurtosis_FrameSize_non-i":"kurtosis_FrameSize_non-i",
                                                                        "QPValuesStatsPerGop_mean_Av_QPBB_non-i":"mean_Av_QPBB_non_i",
                                                                        "QPValuesStatsPerGop_iqr_Av_QPBB_non-i":"iqr_Av_QPBB_non-i",
                                                                        "QPValuesStatsPerGop_kurtosis_Av_QPBB_non-i":"kurtosis_Av_QPBB_non-i",
                                                                        "QPValuesStatsPerGop_iqr_min_QP":"iqr_min_QP",
                                                                        "QPValuesStatsPerGop_std_max_QP_non-i":"std_max_QP_non-i",
                                                                        "AvMotionStatsPerGop_kurtosis_Av_Motion":"kurtosis_Av_Motion",
                                                                        "AvMotionStatsPerGop_0.0_quantil_StdDev_MotionX_non-i":"0.0_quantil_StdDev_MotionX_non-i"})


        feature_columns = list(set(["Bitrate", "Resolution", "Framerate", "mean_Av_QPBB_non_i", "predicted_mos_mode3_baseline", "quant",
                           "1.0_quantil_FrameSize", "kurtosis_Av_Motion", "iqr_Av_QPBB_non-i", "kurtosis_FrameSize_non-i",
                           "std_FrameSize_non-i", "kurtosis_Av_QPBB_non-i", "iqr_min_QP",
                           "0.0_quantil_StdDev_MotionX_non-i", "std_max_QP_non-i", "h264", "hevc", "vp9", "h264_10bit", "hevc_10bit"]))
        feature_columns = sorted(feature_columns)

        prediction_features_rf = prediction_features_rf[sorted(feature_columns)]
        prediction_features_rf = prediction_features_rf.fillna(0)
        residual_mos = residual_rf_model.predict(prediction_features_rf)
        # print("residual_mos = {}".format(residual_mos))

        predicted_score = np.vectorize(map_to_5)(pred)
        predicted_score = predicted_score + residual_mos
        predicted_score = np.clip(predicted_score,1,5)
        prediction_features_rf["rf_pred"] = predicted_score
        final_pred = 0.5 * prediction_features_rf["predicted_mos_mode3_baseline"] + 0.5*prediction_features_rf["rf_pred"]
        return final_pred

    def features_used(self):
        return [
            features.Bitrate,
            features.Framerate,
            features.Resolution,
            features.Codec,
            features.QPValuesStatsPerGop,
            features.BitDepth,
            features.QPstatspersecond,
            features.FramesizeStatsPerGop,
            features.AvMotionStatsPerGop
        ]


    def predict_quality(self,
        videofilename,
        model_config_filename,
        device_type="pc",
        device_resolution="3840x2160",
        viewing_distance="1.5xH",
        display_size=55,
        temporary_folder="tmp"):

        assert_file(videofilename, f"{videofilename} does not exist, please check")
        assert_file(model_config_filename, f"{model_config_filename} does not exist, please check")

        device_type = device_type.lower()
        assert_msg(device_type in DEVICE_TYPES, f"specified device_type '{device_type}' is not supported, only {DEVICE_TYPES} possible")
        assert_msg(device_resolution in DEVICE_RESOLUTIONS, f"specified device_resolution '{device_resolution}' is not supported, only {DEVICE_RESOLUTIONS} possible")
        assert_msg(viewing_distance in VIEWING_DISTANCES, f"specified viewing_distance '{viewing_distance}' is not supported, only {VIEWING_DISTANCES} possible")
        assert_msg(display_size in DISPLAY_SIZES, f"specified display_size '{display_size}' is not supported, only {DISPLAY_SIZES} possible")

        ffprobe_result = ffprobe(videofilename)
        assert_msg(ffprobe_result["codec"] in CODECS_SUPPORTED, f"your video codec is not supported by the model: {ffprobe_result['codec']}")

        with open(model_config_filename) as mfp:
            model_config = json.load(mfp)
        device_type = "pc" if device_type in ["pc", "tv"] else "mobile"

        # select only the required config for the device type
        model_config = model_config[device_type]

        # assume the RF model part is locally stored in the path of model_config_filename
        rf_model = os.path.join(os.path.dirname(model_config_filename), model_config["rf"])

        # load parametertic model coefficients
        model_coefficients = model_config["params"]

        display_res = float(device_resolution.split("x")[0]) * float(device_resolution.split("x")[1])

        self.display_res = display_res

        check_or_install_videoparser()
        os.makedirs(temporary_folder, exist_ok=True)

        feature_cache = os.path.join(
            temporary_folder,
            os.path.splitext(os.path.basename(videofilename))[0] + "_feat.pkl"
        )
        logging.info(f"use feature cache file {feature_cache}")
        if not os.path.isfile(feature_cache):
            # run bitstream parser
            bitstream_parser_result_file = run_bitstream_parser(videofilename, temporary_folder)

            # calculate features
            features = pd.DataFrame([extract_features(videofilename, model.features_used(), ffprobe_result, bitstream_parser_result_file)])
            features.to_pickle(feature_cache)
        else:
            logging.info("features are already cached, extraction skipped")
            features = pd.read_pickle(feature_cache)

        logging.info("features extracted")

        per_sequence = self._calculate(features, model_coefficients, rf_model, display_res, device_type)

        per_second = per_sample_interval_function(
            per_sequence,
            features
        )
        return {
            "per_second": [float(x) for x in per_second],
            "per_sequence": float(per_sequence.values[0])
        }
