from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.schemas.trip_schema import TripRequest, TripPlanResponse
from app.tools.unsplash_tools import get_place_photo
from app.agents import AttractionAgent, WeatherAgent, HotelAgent, PlannerAgent

# 初始化FastAPI应用
app = FastAPI(
    title="旅行助手智能体API",
    description="基于FastAPI+LangChain的智能旅行规划助手",
    version="1.0.0",
    prefix="/api"
)

# 跨域配置（支持前端调用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化所有智能体（单例）
attraction_agent = AttractionAgent()
weather_agent = WeatherAgent()
hotel_agent = HotelAgent()
planner_agent = PlannerAgent()


# ==================== 路由接口 ====================
@app.get("/api/poi/photo", summary="查询景点图片")
def get_poi_photo(name: str):
    """根据景点名称获取Unsplash图片"""
    return get_place_photo(name)


@app.post("/api/trip/plan", summary="生成旅行计划", response_model=TripPlanResponse)
async def generate_trip_plan(request: TripRequest):
    """
    智能旅行规划流程：
    1. 景点搜索 → 2. 天气查询 → 3. 酒店推荐 → 4. 行程规划
    """
    try:
        print("搜索景点")
        # 第一步：搜索景点
        attractions = attraction_agent.search_attractions(request.city, request.preferences)
        print(f"attractions:{attractions}")
        print("查询天气")
        # 第二步：查询天气
        weather = weather_agent.get_city_weather(request.city)
        print(f"weather:{weather}")
        print("搜索酒店")
        # 第三步：搜索酒店
        hotels = hotel_agent.search_hotels(request.city)
        print(f"搜索酒店:{hotels}")
        print("生成行程计划")
        # 第四步：生成行程计划
        trip_plan = planner_agent.generate_plan(request, attractions, weather, hotels)
        print(f"生成计划:{trip_plan}")
        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"旅行规划失败: {str(e)}")


# 健康检查
@app.get("/api/health", summary="服务健康检查")
def health_check():
    return {"status": "ok", "message": "旅行助手服务运行正常"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)