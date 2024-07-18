from botocore.exceptions import ClientError
from PIL import Image
from urllib import parse
import boto3

from typing import List, Tuple, Optional
import base64
import io

# Lambda@Edge 에서는 환경 변수의 사용이 불가능하기 때문에 직접 코드 내에서 지정을 해야 한다.
s3_bucket_name: str = "img.springjh.kr"
s3_client = boto3.client("s3")


def lambda_handler(event: dict, context) -> dict:
    if "Records" not in event or len(event["Records"]) == 0:
        return {
            "status": 400,
            "statusDescription": "Bad Request",
            "headers": {
                "content-type": [
                    {"key": "Content-Type", "value": "text/plain"}
                ]
            },
            "body": "Invalid event structure: 'Records' key is missing"
        }

    record: dict = event["Records"][0]["cf"]
    request: dict = record["request"]

    target_width: int = 0
    target_height: int = 0
    target_quality: int = 75
    target_size: Optional[str] = None

    # 변환 대상 값을 parsing
    if request["querystring"] != "":
        queries: List[Tuple[str, str]] = [
            tuple(q_str.split("=")) for q_str in request["querystring"].split("&")
        ]
        for k, v in queries:
            if k == "w":
                target_width = int(v)
            elif k == "h":
                target_height = int(v)
            elif k == "q":
                target_quality = int(v)
                if target_quality > 95:
                    target_quality = 95
                elif target_quality < 1:
                    target_quality = 1
            elif k == "s":
                target_size = v

    if target_width == 0 and target_height == 0 and target_size is not None:
        if target_size == "s":
            target_width = 200
        elif target_size == "m":
            target_width = 400
        elif target_size == "l":
            target_width = 600

    if target_width == 0 and target_height == 0:
        return {
            "status": 200,
            "statusDescription": "OK",
            "headers": request["headers"]
        }

    qs: str = f"q{target_quality}_"
    if target_width != 0:
        qs = f"w{target_width}{qs}"

    if target_height != 0:
        qs = f"h{target_height}{qs}"

    s3_object_key: str = request["uri"][1:]
    s3_object_key_split: List[str] = s3_object_key.split("/")
    s3_object_key_split[-1] = qs + s3_object_key_split[-1]

    # Lambda@Edge 에서는 Body Size 가 1MB 를 넘을 수 없다.
    # 따라서 변환 결과물이 1MB 가 넘을 경우 s3 에 해당 결과물을 올리게 된다.
    # converted_object_key 는 해당 결과물의 파일명에 해당함.
    converted_object_key: str = "/".join(s3_object_key_split)

    # 변환 결과물이 이미 존재하는지 확인.
    is_converted_object_exists: bool = True
    try:
        s3_response = s3_client.head_object(
            Bucket=s3_bucket_name, Key=parse.unquote(converted_object_key)
        )
    except ClientError:
        is_converted_object_exists = False

    if is_converted_object_exists is True:
        # 변환 결과물이 이미 있는 경우 해당하는 파일의 링크를 301 redirect 로 넘겨준다.
        return {
            "status": 301,
            "statusDescription": "Moved Permanently",
            "headers": {
                "location": [
                    {"key": "Location", "value": f"/{converted_object_key}"}
                ]
            }
        }

    try:
        s3_response = s3_client.get_object(Bucket=s3_bucket_name, Key=parse.unquote(s3_object_key))
    except ClientError as e:
        raise e

    # JPEG 나 PNG 가 아닐 경우 pass through
    s3_object_type: str = s3_response["ContentType"]
    if s3_object_type not in ["image/jpeg", "image/png"]:
        return {
            "status": 200,
            "statusDescription": "OK",
            "headers": request["headers"],
            "body": s3_response["Body"].read().decode("utf-8"),
            "bodyEncoding": "text"
        }

    # 원래 이미지 불러오기
    original_image: Image = Image.open(s3_response["Body"])
    width, height = original_image.size

    w_decrease_ratio: float = target_width / width
    h_decrease_ratio: float = target_height / height

    # 축소 비율이 덜한 쪽으로 기준을 잡는다.
    transform_ratio: float = max(w_decrease_ratio, h_decrease_ratio)
    if transform_ratio > 1.0:
        transform_ratio = 1.0

    converted_image: Image = original_image.resize(
        (int(width * transform_ratio), int(height * transform_ratio)),
        reducing_gap=3,
    )

    if target_width == 0:
        target_width = int(width * transform_ratio)

    if target_height == 0:
        target_height = int(height * transform_ratio)

    mid_x: float = converted_image.size[0] / 2
    mid_y: float = converted_image.size[1] / 2
    diff_x: float = target_width / 2
    diff_y: float = target_height / 2

    start_x: int = int(round(mid_x - diff_x))
    if start_x < 0:
        start_x = 0

    start_y: int = int(round(mid_y - diff_y))
    if start_y < 0:
        start_y = 0

    end_x: int = int(round(mid_x + diff_x))
    if end_x >= converted_image.size[0]:
        end_x = converted_image.size[0] - 1

    end_y: int = int(round(mid_y + diff_y))
    if end_y >= converted_image.size[1]:
        end_y = converted_image.size[1] - 1

    cropped_image: Image = converted_image.crop((start_x, start_y, end_x, end_y))

    # https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.tobytes
    # 위 링크에서는 Compressed Image 에서 .tobytes() 사용 시 이미지가 제대로 저장되지 않는다고 하고 있음.
    bytes_io = io.BytesIO()

    # PNG 일 경우 quality option 은 무시됨.
    cropped_image.save(bytes_io, format=original_image.format, optimize=True, quality=target_quality)
    result_size: int = bytes_io.tell()
    result_data: bytes = bytes_io.getvalue()
    result: str = base64.standard_b64encode(result_data).decode()
    bytes_io.close()

    converted_image.close()
    original_image.close()

    if result_size > 1000 * 1000:
        # 결과물이 1MB 를 넘을 경우 (정확히는 1024 * 1024 로 해야 하지만 혹시 모르니..)
        # 결과물을 S3 에 넣은 후 해당 파일의 링크를 301 redirect 로 넘겨준다.
        try:
            s3_response = s3_client.put_object(
                Bucket=s3_bucket_name,
                Key=parse.unquote(converted_object_key),
                ContentType=s3_object_type,
                Body=result_data,
            )
        except ClientError as e:
            raise e

        return {
            "status": 301,
            "statusDescription": "Moved Permanently",
            "headers": {
                "location": [
                    {"key": "Location", "value": f"/{converted_object_key}"}
                ]
            }
        }
    else:
        # 1MB 미만이라면 결과값을 그대로 response body 에 넣어서 보내준다.
        return {
            "status": 200,
            "statusDescription": "OK",
            "headers": {
                "content-type": [
                    {"key": "Content-Type", "value": s3_object_type}
                ]
            },
            "body": result,
            "bodyEncoding": "base64"
        }
