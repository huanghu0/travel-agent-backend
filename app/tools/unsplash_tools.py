import os
import requests
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY")


def get_place_photo(place_name: str) -> dict:
    """查询景点图片（Unsplash API）"""
    url = "https://api.unsplash.com/search/photos"
    params = {
        "query": place_name,
        "client_id": UNSPLASH_KEY,
        "per_page": 5
    }

    try:
        response = requests.get(url, params=params)
        data = response.json()

        if data.get("results"):
            photo = data["results"][0]
            return {
                "success": True,
                "message": "获取图片成功",
                "data": {
                    "name": place_name,
                    "photo_url": photo["urls"]["regular"]
                },
            }
        return {"place_name": place_name, "image_url": None, "error": "未找到图片"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片查询失败: {str(e)}")