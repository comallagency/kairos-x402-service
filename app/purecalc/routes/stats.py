"""Statistics pure-compute routes - stdlib `statistics` module plus a
hand-written OLS regression and linear-interpolation percentile."""

import statistics

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register


# ---------------------------------------------------------------- stats/summary
class SummaryInput(BaseModel):
    data: list[float] = Field(min_length=1)


class SummaryOutput(BaseModel):
    count: int
    mean: float
    median: float
    stdev: float | None
    variance: float | None
    min: float
    max: float


def compute_summary(inp: SummaryInput) -> SummaryOutput:
    data = inp.data
    stdev = round(statistics.stdev(data), 6) if len(data) > 1 else None
    variance = round(statistics.variance(data), 6) if len(data) > 1 else None
    return SummaryOutput(
        count=len(data), mean=round(statistics.mean(data), 6), median=statistics.median(data),
        stdev=stdev, variance=variance, min=min(data), max=max(data),
    )


register(ComputeSpec(
    slug="stats/summary", price="$0.001", service_name="stats-summary",
    description="Descriptive statistics for a list of numbers: count, mean, median, sample stdev/variance, min, max.",
    tags=["statistics", "summary statistics", "mean", "median", "standard deviation"],
    input_model=SummaryInput, output_model=SummaryOutput, compute=compute_summary,
    sample_input={"data": [2, 4, 4, 4, 5, 5, 7, 9]},
    sample_output={"count": 8, "mean": 5.0, "median": 4.5, "stdev": 2.138090, "variance": 4.571429, "min": 2.0, "max": 9.0},
))


# ------------------------------------------------------------ stats/correlation
class CorrelationInput(BaseModel):
    x: list[float] = Field(min_length=2)
    y: list[float] = Field(min_length=2)


class CorrelationOutput(BaseModel):
    pearson_r: float


def compute_correlation(inp: CorrelationInput) -> CorrelationOutput:
    if len(inp.x) != len(inp.y):
        raise ComputeError("length_mismatch", "x and y must have the same length")
    try:
        r = statistics.correlation(inp.x, inp.y)
    except statistics.StatisticsError as exc:
        raise ComputeError("undefined_correlation", str(exc)) from exc
    return CorrelationOutput(pearson_r=round(r, 6))


register(ComputeSpec(
    slug="stats/correlation", price="$0.001", service_name="stats-correlation",
    description="Pearson correlation coefficient between two equal-length numeric series.",
    tags=["statistics", "correlation", "pearson", "data analysis"],
    input_model=CorrelationInput, output_model=CorrelationOutput, compute=compute_correlation,
    sample_input={"x": [1, 2, 3, 4, 5], "y": [2, 4, 6, 8, 10]},
    sample_output={"pearson_r": 1.0},
))


# -------------------------------------------------------------- stats/regression
class RegressionInput(BaseModel):
    x: list[float] = Field(min_length=2)
    y: list[float] = Field(min_length=2)


class RegressionOutput(BaseModel):
    slope: float
    intercept: float
    r_squared: float


def compute_regression(inp: RegressionInput) -> RegressionOutput:
    if len(inp.x) != len(inp.y):
        raise ComputeError("length_mismatch", "x and y must have the same length")
    xs, ys = inp.x, inp.y
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denominator = sum((xi - mean_x) ** 2 for xi in xs)
    if denominator == 0:
        raise ComputeError("degenerate_input", "all x values are identical - slope is undefined")

    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(xs, ys))
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    ss_tot = sum((yi - mean_y) ** 2 for yi in ys)
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(xs, ys))
    r_squared = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot

    return RegressionOutput(slope=round(slope, 6), intercept=round(intercept, 6), r_squared=round(r_squared, 6))


register(ComputeSpec(
    slug="stats/regression", price="$0.001", service_name="stats-regression",
    description="Ordinary least squares simple linear regression (y = slope*x + intercept) with R-squared.",
    tags=["statistics", "linear regression", "least squares", "r squared", "data analysis"],
    input_model=RegressionInput, output_model=RegressionOutput, compute=compute_regression,
    sample_input={"x": [1, 2, 3, 4, 5], "y": [2, 4, 6, 8, 10]},
    sample_output={"slope": 2.0, "intercept": 0.0, "r_squared": 1.0},
))


# -------------------------------------------------------------- stats/percentile
class PercentileInput(BaseModel):
    data: list[float] = Field(min_length=1)
    percentile: float = Field(ge=0, le=100)


class PercentileOutput(BaseModel):
    value: float


def compute_percentile(inp: PercentileInput) -> PercentileOutput:
    s = sorted(inp.data)
    n = len(s)
    if n == 1:
        return PercentileOutput(value=s[0])
    rank = (inp.percentile / 100) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    value = s[lo] + (s[hi] - s[lo]) * frac
    return PercentileOutput(value=round(value, 6))


register(ComputeSpec(
    slug="stats/percentile", price="$0.001", service_name="stats-percentile",
    description="Nth percentile of a list of numbers (linear interpolation between closest ranks).",
    tags=["statistics", "percentile", "quantile", "data analysis"],
    input_model=PercentileInput, output_model=PercentileOutput, compute=compute_percentile,
    sample_input={"data": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "percentile": 50},
    sample_output={"value": 5.5},
))
