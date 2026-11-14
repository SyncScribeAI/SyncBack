from typing import Protocol

from app.domain.interfaces.file_storage import UploadContent
from app.domain.source_files import SourceFileReport


class SourceFileInspector(Protocol):
    """Проверка содержимого файла источника до записи в хранилище.

    Отказ поднимается доменной ошибкой с причиной и способом действия; поток после
    проверки должен стоять в начале, чтобы хранилище прочитало файл целиком.
    """

    async def inspect(self, filename: str, content: UploadContent) -> SourceFileReport: ...
