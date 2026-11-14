"""Положение правки или флага на страницах PDF."""

from pydantic import BaseModel, Field


class PageGeometryResponse(BaseModel):
    rects: list[list[float]] = Field(
        description=(
            "Прямоугольники [page, x0, y0, x1, y1]: page с единицы, координаты в точках в "
            "пространстве страницы PDF (начало внизу слева, без учёта CropBox и /Rotate), "
            "как у аннотаций. У вставки это символ перед точкой вставки"
        )
    )
