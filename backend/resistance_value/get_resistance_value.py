from enum import Enum
from functools import reduce
from typing import *
import numpy as np
import onnxruntime
from mmcv import Config
import cv2, torch

####################################################
# config 파일 및 checkpoint 파일 경로
# 반드시 설정 필요
####################################################
model_path: str = "model/resistor_value_model.onnx"
model: onnxruntime = None

####################################################
# 416x416x3 크기의 저항 이미지를 주면, 해당 저항값을 반환한다.
#
# 최초 실행 시에는 딥러닝 모델을 로드해야 해서 실행이 느리다.
# 미리 로드하고 싶다면, 아래 load_resistor_color_detection_model을 미리 호출하자.
####################################################


def data_pipeline(img: np.ndarray, model_cfg_path: str) -> torch.Tensor:
    cfg = Config.fromfile(model_cfg_path)

    transforms = None
    for pipeline in cfg.test_pipeline:
        if "transforms" in pipeline:
            transforms = pipeline["transforms"]
            break
    assert transforms is not None, "Failed to find `transforms`"
    norm_config_li = [_ for _ in transforms if _["type"] == "Normalize"]
    assert len(norm_config_li) == 1, "`norm_config` should only have one"
    norm_config = norm_config_li[0]

    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img_scale = (1333, 800)
    input_shape = (1, 3, img_scale[1], img_scale[0])

    img = cv2.resize(img, input_shape[2:][::-1])
    mean = np.array(norm_config["mean"], dtype=np.float32)
    std = np.array(norm_config["std"], dtype=np.float32)

    normalized = (img - mean) / std
    img = normalized.transpose(2, 0, 1)
    img = torch.from_numpy(img).unsqueeze(0).float().requires_grad_(True)

    return img.cpu().detach().numpy()


def get_resistance_value(img: np.ndarray) -> float:
    model = load_resistor_color_detection_model()

    model_cfg_path = "model/mask_rcnn_r50_fpn_1x_coco_resistor_color.py"
    input_arr = data_pipeline(img, model_cfg_path)

    result = (sess_run := model.run([], {"input": input_arr}))[0]
    categories = sess_run[1][0]

    bands = result_to_bands(result, categories)
    if len(bands) == 0:
        return 100  # model이 저항띠를 하나도 못찾으면 대충 100옴이라 하자

    bands = dimension_compress(bands)
    bands = sort_band(bands)

    if len(bands) < 4:
        bands = add_dummy_band(bands)
    elif len(bands) > 4:
        bands = remove_fake_band(bands)

    colors = list(map(lambda band: band.color, bands))
    return colors_to_value(colors)


####################################################
# 저항띠를 추출하는 모델을 로드한다.
####################################################
def load_resistor_color_detection_model() -> onnxruntime.InferenceSession:
    global model
    global model_path

    if model == None:
        model = onnxruntime.InferenceSession(model_path)

    return model


class ResistorColor(Enum):
    Black = 0
    Blue = 1
    Brown = 2
    Green = 3
    Orange = 4
    Red = 5
    Side_Gold = 6
    Side_Silver = 7
    Yellow = 8


def is_side(color: ResistorColor):
    return color == ResistorColor.Side_Gold or color == ResistorColor.Side_Silver


class Band_xy:
    color: ResistorColor
    x: float
    y: float
    acc: float

    def __init__(self, color: ResistorColor, x: float, y: float, acc: float):
        self.color = color
        self.x = x
        self.y = y
        self.acc = acc


class Band_x:
    color: ResistorColor
    x: float
    acc: float

    def __init__(self, color: ResistorColor, x: float, acc: float):
        self.color = color
        self.x = x
        self.acc = acc


def result_to_bands(
    # img,
    result: list,
    categories: list,
) -> List[Band_xy]:
    bands = list()

    # color_table = {
    #     0: (10, 10, 10),
    #     1: (100, 100, 100),
    #     2: (255, 100, 198),
    #     3: (59, 189, 77),
    #     4: (157, 59, 203),
    #     5: (200, 210, 50),
    #     6: (23, 125, 99),
    # }

    for lines in result:
        for line, category in zip(enumerate(lines), categories):
            line = line[1]
            x1 = line[0]
            y1 = line[1]
            x2 = line[2]
            y2 = line[3]
            acc = line[4]

            # cv2.rectangle(
            #     img,
            #     (int(x1 * (img.shape[1] / 1333)), int(y1 * (img.shape[0] / 800))),
            #     (int(x2 * (img.shape[1] / 1333)), int(y2 * (img.shape[0] / 800))),
            #     color_table[category],
            #     2,
            # )

            # if acc != 0:
            #     print(category, [x1, y1, x2, y2, acc])
            bands.append(
                Band_xy(ResistorColor(category), (x1 + x2) / 2, (y1 + y2) / 2, acc)
            )

    # cv2.imshow("img", img)
    # cv2.waitKeyEx(0)
    # cv2.destroyAllWindows()
    return bands


def dimension_compress(bands: List[Band_xy]) -> List[Band_x]:
    diff_x, diff_y = diff_xy(bands)
    if diff_x > diff_y:
        remain_x = lambda band: Band_x(band.color, band.x, band.acc)
        return list(map(remain_x, bands))
    else:
        remain_y = lambda band: Band_x(band.color, band.y, band.acc)
        return list(map(remain_y, bands))


def diff_xy(bands: List[Band_xy]) -> Tuple[float, float]:
    two_min = lambda a, b: tuple([min(a[0], b.x), min(a[1], b.y)])
    two_max = lambda a, b: tuple([max(a[0], b.x), max(a[1], b.y)])
    minmax = lambda a, b: tuple([two_min(a[0], b), two_max(a[1], b)])

    if len(bands) == 0:
        raise Exception("length of bands is 0")
    b0 = bands[0]
    initial = [[b0.x, b0.y], [b0.x, b0.y]]
    [min_x, min_y], [max_x, max_y] = reduce(minmax, bands, initial)
    return [max_x - min_x, max_y - min_y]


# x값 기준으로 정렬하되, Side색은 마지막에 오도록 한다.
# Side색이 없는 경우, 마지막에 Side-Gold를 임의로 추가한다.
# Side색이 여럿인 경우, acc값이 제일 높은 Side색을 제외한 다른 Side 색을 제거한다.
# Side색이 끝에 있지 않은 경우, Side색이 끝에 오도록 다른 색을 제거한다.
def sort_band(bands: List[Band_x]) -> List[Band_x]:
    side_count = reduce(
        lambda val, band: val + 1 if is_side(band.color) else val, bands, 0
    )

    if side_count < 1:
        bands.append(Band_x(ResistorColor.Side_Gold, float("inf"), 0.0))
    elif side_count > 1:
        side_bands = list(filter(lambda band: is_side(band.color), bands))
        not_side_bands = list(filter(lambda band: not is_side(band.color), bands))
        sorted_side_bands = sorted(side_bands, key=lambda band: band.acc, reverse=True)
        bands = not_side_bands + [sorted_side_bands[0]]

    sorted_bands = sorted(bands, key=lambda band: band.x)
    side_index = 0
    for index, band in enumerate(sorted_bands):
        if is_side(band.color):
            side_index = index

    if side_index < len(sorted_bands) / 2:
        sorted_bands = list(reversed(sorted_bands))
        side_index = len(sorted_bands) - 1 - side_index

    return sorted_bands[0 : side_index + 1]


# 저항띠가 4개 미만인 경우, Side띠 반대편에 Brown띠를 여렷 추가하여 총 저항띠가 4개가 되도록 한다.
def add_dummy_band(bands: List[Band_x]) -> List[Band_x]:
    absence = 4 - len(bands)
    imsi_bands = [Band_x(ResistorColor.Brown, -1.0, 0.0)] * absence
    return imsi_bands + bands


# 저항띠가 4개 초과인 경우, 아래 프로세스를 반복하여 총 저항띠가 4개가 될 때까지 저항띠를 줄인다.
# - 두 저항띠 쌍 중 제일 인접한 저항띠 쌍을 고른다.
# - 그 중에 Side띠가 있다면, 다른 띠를 없앤다.
# - 그 중에 Side띠가 없다면, acc값이 작은 띠를 없앤다.
def remove_fake_band(bands: List[Band_x]) -> List[Band_x]:
    while len(bands) > 4:
        nearest_index = 0
        nearest_dist = abs(bands[1].x - bands[0].x)
        for i in range(1, len(bands) - 1):
            dist = abs(bands[i + 1].x - bands[i].x)
            if dist < nearest_dist:
                nearest_index = i
                nearest_dist = dist

        i = nearest_index
        if is_side(bands[i].color):
            del bands[i + 1]
        elif is_side(bands[i + 1].color):
            del bands[i]
        else:
            if bands[i].acc < bands[i + 1].acc:
                del bands[i]
            else:
                del bands[i + 1]
    return bands


def colors_to_value(colors: List[ResistorColor]) -> float:
    first = front_color_to_value(colors[0])
    second = front_color_to_value(colors[1])
    third = color_to_multiplier(colors[2])

    return (first * 10 + second) * third


def front_color_to_value(color: ResistorColor) -> int:
    value_table = {
        ResistorColor.Black: 0,
        ResistorColor.Brown: 1,
        ResistorColor.Red: 2,
        ResistorColor.Orange: 3,
        ResistorColor.Yellow: 4,
        ResistorColor.Green: 5,
        ResistorColor.Blue: 6
        # ResistorColor.Violet : 7,
        # ResistorColor.Gray : 8,
        # ResistorColor.White : 9
    }
    return value_table.get(color, 0)


def color_to_multiplier(color: ResistorColor) -> float:
    multiplier_table = {
        ResistorColor.Black: 1,
        ResistorColor.Brown: 10,
        ResistorColor.Red: 100,
        ResistorColor.Orange: 1000,
        ResistorColor.Yellow: 10000,
        ResistorColor.Green: 100000,
        ResistorColor.Blue: 1000000
        # ResistorColor.Violet : 10000000,
        # ResistorColor.Gold   : 0.1,
        # ResistorColor.Silver : 0.01
    }
    return multiplier_table.get(color, 1)


if __name__ == "__main__":
    img_dir = "images/Resistor/resbody-527_jpg.rf.e7ef8bffdf175603fd95b237fb16e9aa.jpg"
    img = cv2.imread(img_dir, cv2.IMREAD_COLOR)

    ohm = get_resistance_value(img)
    print(ohm)
