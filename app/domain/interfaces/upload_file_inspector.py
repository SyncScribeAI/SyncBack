from dataclasses import dataclass
from typing import Protocol

from app.domain.interfaces.file_storage import UploadContent


@dataclass(frozen=True, slots=True)
class UploadFileReport:
    """Итог проверки файла при загрузке.

    pages_without_text: страницы PDF без текстового слоя (с единицы). Файл принят, но
    текст этих страниц в анализ не попадёт, поэтому пользователь видит их номера сразу.
    """

    pages_without_text: tuple[int, ...] = ()


class UploadFileInspector(Protocol):
    """Проверка содержимого загружаемого файла (документа или источника) до записи в хранилище.

    Отказ поднимается доменной ошибкой с причиной и способом действия; поток после
    проверки должен стоять в начале, чтобы хранилище прочитало файл целиком.
    """

    async def inspect(self, filename: str, content: UploadContent) -> UploadFileReport: ...
