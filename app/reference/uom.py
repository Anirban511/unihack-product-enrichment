"""Unit-of-measure standardisation.

Rules enforced (guide s.2, Unilog_Master_UOM_Standards_Abbreviations_and_Terms):
  * one approved abbreviation per measurement type ("inches"/"IN."/"inch" -> "in")
  * always a space between the number and the unit  ->  "24 in", never "24in"
  * the unit lives in its own column, next to the value
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from app.reference.base import column_like, find_reference, promote_header, read_sheets
from app.textnorm import clean, split_number_unit, to_trade_fraction

# measurement type -> (approved abbreviation, [spellings seen in the wild])
_SEED: Dict[str, Tuple[str, List[str]]] = {
    "Length": ("in", ["in", "in.", "inch", "inches", '"', "''", "ins"]),
    "Length (foot)": ("ft", ["ft", "ft.", "foot", "feet", "'"]),
    "Length (yard)": ("yd", ["yd", "yard", "yards"]),
    "Length (mile)": ("mi", ["mi", "mile", "miles"]),
    "Length (mm)": ("mm", ["mm", "millimeter", "millimetre", "millimeters", "millimetres"]),
    "Length (cm)": ("cm", ["cm", "centimeter", "centimetre", "centimeters", "centimetres"]),
    "Length (m)": ("m", ["m", "meter", "metre", "meters", "metres"]),
    "Voltage": ("V", ["v", "volt", "volts", "vac", "vdc", "voltage"]),
    "Voltage (kV)": ("kV", ["kv", "kilovolt", "kilovolts"]),
    "Voltage (mV)": ("mV", ["mv", "millivolt", "millivolts"]),
    "Current": ("A", ["a", "amp", "amps", "ampere", "amperes", "amperage"]),
    "Current (mA)": ("mA", ["ma", "milliamp", "milliampere", "milliamps"]),
    "Power": ("W", ["w", "watt", "watts", "wattage"]),
    "Power (kW)": ("kW", ["kw", "kilowatt", "kilowatts"]),
    "Power (hp)": ("hp", ["hp", "horsepower", "h.p."]),
    "Energy": ("kW-hr", ["kwh", "kw-hr", "kw hr", "kilowatt hour", "kilowatt-hour", "kwhr"]),
    "Frequency": ("Hz", ["hz", "hertz", "cycles"]),
    "Frequency (kHz)": ("kHz", ["khz", "kilohertz"]),
    "Frequency (MHz)": ("MHz", ["mhz", "megahertz"]),
    "Resistance": ("ohm", ["ohm", "ohms", "Ω"]),
    "Capacitance": ("uF", ["uf", "µf", "microfarad", "microfarads"]),
    "Sound": ("dBA", ["dba", "db(a)", "decibel", "decibels", "db"]),
    "Weight (lb)": ("lb", ["lb", "lbs", "lb.", "pound", "pounds"]),
    "Weight (oz)": ("oz", ["oz", "ounce", "ounces"]),
    "Weight (kg)": ("kg", ["kg", "kilogram", "kilograms", "kgs"]),
    "Weight (g)": ("g", ["g", "gram", "grams", "gm"]),
    "Weight (ton)": ("ton", ["ton", "tons", "tonne", "tonnes"]),
    "Pressure": ("psi", ["psi", "p.s.i.", "pounds per square inch"]),
    "Pressure (bar)": ("bar", ["bar", "bars"]),
    "Pressure (kPa)": ("kPa", ["kpa", "kilopascal", "kilopascals"]),
    "Pressure (inHg)": ("in Hg", ["inhg", "in hg", "inches of mercury"]),
    "Pressure (psig)": ("psig", ["psig"]),
    "Flow": ("gpm", ["gpm", "g.p.m.", "gallons per minute", "gal/min"]),
    "Flow (gph)": ("gph", ["gph", "gallons per hour", "gal/hr"]),
    "Flow (cfm)": ("cfm", ["cfm", "c.f.m.", "cubic feet per minute"]),
    "Flow (lpm)": ("lpm", ["lpm", "liters per minute", "l/min"]),
    "Volume (gal)": ("gal", ["gal", "gallon", "gallons"]),
    "Volume (qt)": ("qt", ["qt", "quart", "quarts"]),
    "Volume (L)": ("L", ["l", "liter", "litre", "liters", "litres"]),
    "Volume (mL)": ("mL", ["ml", "milliliter", "millilitre"]),
    "Volume (cu ft)": ("cu ft", ["cu ft", "cuft", "ft3", "cubic feet", "cubic foot", "ft³"]),
    "Volume (cu in)": ("cu in", ["cu in", "cuin", "in3", "cubic inches", "in³"]),
    "Area (sq ft)": ("sq ft", ["sq ft", "sqft", "ft2", "square feet", "ft²"]),
    "Area (sq in)": ("sq in", ["sq in", "sqin", "in2", "square inches", "in²"]),
    "Temperature (F)": ("°F", ["f", "deg f", "degf", "°f", "degrees f", "fahrenheit"]),
    "Temperature (C)": ("°C", ["c", "deg c", "degc", "°c", "degrees c", "celsius"]),
    "Angle": ("°", ["deg", "degree", "degrees", "°"]),
    "Speed (rpm)": ("rpm", ["rpm", "r.p.m.", "revolutions per minute"]),
    "Speed (fpm)": ("fpm", ["fpm", "feet per minute", "ft/min"]),
    "Speed (mph)": ("mph", ["mph", "miles per hour"]),
    "Speed (spm)": ("spm", ["spm", "strokes per minute"]),
    "Speed (opm)": ("opm", ["opm", "orbits per minute", "oscillations per minute"]),
    "Torque (in-lb)": ("in-lb", ["in-lb", "in lb", "inch pounds", "in.lbs", "inlb", "in-lbs"]),
    "Torque (ft-lb)": ("ft-lb", ["ft-lb", "ft lb", "foot pounds", "ft.lbs", "ftlb", "ft-lbs"]),
    "Torque (Nm)": ("N-m", ["nm", "n-m", "newton meter", "newton metre", "newton-meters"]),
    "Time (hr)": ("hr", ["hr", "hrs", "hour", "hours", "h"]),
    "Time (min)": ("min", ["min", "mins", "minute", "minutes"]),
    "Time (sec)": ("sec", ["sec", "secs", "second", "seconds", "s"]),
    "Count": ("ea", ["ea", "each", "pc", "pcs", "piece", "pieces", "unit", "units"]),
    "Pack": ("pk", ["pk", "pack", "packs"]),
    "Box": ("bx", ["bx", "box", "boxes"]),
    "Case": ("cs", ["cs", "case", "cases"]),
    "Roll": ("rl", ["rl", "roll", "rolls"]),
    "Percent": ("%", ["%", "percent", "pct"]),
    "Grit": ("grit", ["grit", "grits"]),
    "Gauge": ("ga", ["ga", "gauge", "gage", "awg"]),
    "Thread (tpi)": ("tpi", ["tpi", "threads per inch"]),
    "BTU": ("BTU", ["btu", "btus", "british thermal unit"]),
    "BTU/hr": ("BTU/hr", ["btu/hr", "btuh", "btu per hour", "btu/h"]),
    "Lumens": ("lm", ["lm", "lumen", "lumens"]),
    "Kelvin": ("K", ["k", "kelvin"]),
    "Candela": ("cd", ["cd", "candela"]),
    "Lux": ("lx", ["lx", "lux"]),
    "Density": ("lb/ft3", ["lb/ft3", "pcf", "pounds per cubic foot"]),
    "Viscosity": ("cSt", ["cst", "centistokes"]),
    "Micron": ("micron", ["micron", "microns", "µm", "um"]),
    "Mil": ("mil", ["mil", "mils"]),
    "Newton": ("N", ["n", "newton", "newtons"]),
    "Joule": ("J", ["j", "joule", "joules"]),
    "Amp-hour": ("Ah", ["ah", "amp hour", "amp-hour", "ampere hour"]),
    "VA": ("VA", ["va", "volt-ampere", "volt amperes"]),
    "kVA": ("kVA", ["kva", "kilovolt-ampere"]),
    "Cycles": ("cyc", ["cyc", "cycle", "cycles"]),
    "Phase": ("ph", ["ph", "phase", "phases"]),
    "NPT": ("NPT", ["npt", "n.p.t."]),
    "Mesh": ("mesh", ["mesh"]),
    "Ply": ("ply", ["ply", "plies"]),
}


class UomStore:
    """Approved-abbreviation resolver. The real workbook wins if it is present."""

    def __init__(self) -> None:
        self.source = "bootstrap:published-standards"
        self.canonical: Dict[str, str] = {}   # normalised spelling -> approved abbrev
        self.approved: set = set()
        for _mt, (abbrev, spellings) in _SEED.items():
            self.approved.add(abbrev)
            for sp in list(spellings) + [abbrev]:
                self.canonical.setdefault(self._k(sp), abbrev)
        self._load_workbook()

    @staticmethod
    def _k(s: str) -> str:
        return re.sub(r"[\s.]+", "", clean(s).lower())

    def _load_workbook(self) -> None:
        path = find_reference("uom", "standards")
        if not path:
            return
        try:
            for _name, raw in read_sheets(path).items():
                df = promote_header(raw, ["measurement", "abbreviation", "capture", "example"])
                if df is None:
                    continue
                c_abbrev = column_like(df, "approved abbreviation", "abbreviation", "uom", "symbol")
                c_term = column_like(df, "term", "unit", "measurement", "spelling", "capture form")
                if not c_abbrev:
                    continue
                for _i, row in df.iterrows():
                    ab = clean(row.get(c_abbrev, ""))
                    if not ab:
                        continue
                    self.approved.add(ab)
                    self.canonical[self._k(ab)] = ab
                    if c_term:
                        for piece in re.split(r"[|,;/]", clean(row.get(c_term, ""))):
                            if piece.strip():
                                self.canonical[self._k(piece)] = ab
            self.source = "workbook:" + path.name
        except Exception as exc:  # a malformed drop-in must never kill the API
            self.source = "bootstrap (workbook {} unreadable: {})".format(path.name, exc)

    # -- api -------------------------------------------------------------
    def canonicalise(self, unit: Optional[str]) -> str:
        u = clean(unit)
        if not u:
            return ""
        return self.canonical.get(self._k(u), u)

    def is_approved(self, unit: str) -> bool:
        return clean(unit) in self.approved

    def format_measure(self, value: str, unit: str) -> str:
        """'24' + 'inches' -> '24 in'. The space is mandatory."""
        v, u = clean(value), self.canonicalise(unit)
        if not v:
            return ""
        if not u:
            return v
        return v + u if u in {"%", "°"} else v + " " + u

    def split(self, text: str) -> Tuple[str, str]:
        """Split a scraped measurement into (value, approved-uom), fraction-normalised.

        '50.25 inches' -> ('50-1/4', 'in')  |  'Leg' -> ('Leg', '')
        A token that does not resolve to an approved abbreviation is left whole
        rather than guessed at - an unknown unit is a data problem, not a licence
        to invent one.
        """
        raw_v, raw_u = split_number_unit(text)
        if not raw_v:
            return clean(text), ""
        if raw_u:
            unit = self.canonical.get(self._k(raw_u), "")
            if not unit:
                return clean(text), ""
            return to_trade_fraction(raw_v), unit
        if re.fullmatch(r"-?[\d./\-]+", raw_v):
            return to_trade_fraction(raw_v), ""
        return clean(text), ""

    def status(self) -> dict:
        return {
            "source": self.source,
            "approved_abbreviations": len(self.approved),
            "accepted_spellings": len(self.canonical),
        }


UOM = UomStore()
