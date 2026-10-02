from .csv_import import CsvImport
from .pima_jp_calendar import PimaJpCalendar
from .tucson_code_cases import TucsonCodeCases

SOURCES = {s.name: s for s in (TucsonCodeCases, PimaJpCalendar, CsvImport)}

# Sources that run with no input files; ``leadgen fetch`` with no --source
# runs these.
AUTOMATIC = ("tucson_code_cases",)
