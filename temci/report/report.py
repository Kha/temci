import logging
from enum import Enum, unique

import math
import re
import shutil
from collections import namedtuple

import multiprocessing

import time

import sys

import itertools

from scipy import stats

from temci.report.stats import TestedPairsAndSingles, BaseStatObject, TestedPair, TestedPairProperty, StatMessage, \
    StatMessageType, Single, SingleProperty, SinglesProperty
from temci.report.testers import TesterRegistry, Tester
from temci.report.rundata import RunDataStatsHelper, RunData, ExcludedInvalidData
from temci.run.run_driver import filter_runs
from temci.utils.sudo_utils import chown
from temci.utils.typecheck import *
from temci.utils.registry import AbstractRegistry, register
import temci.utils.util as util
import click, os
import yaml
if util.can_import("numpy"):
    import numpy as np
    import pandas as pd
    import matplotlib as mpl
    mpl.use("agg")
from temci.utils.settings import Settings
from multiprocessing import Pool
from temci.utils.util import join_strs
import typing as t
from temci.utils.number import format_number, FNumber, fnumber, ParenthesesMode


class ReporterRegistry(AbstractRegistry):
    """
    Registry for reporters.
    """

    settings_key_path = "report"
    use_key = "reporter"
    use_list = False
    default = "console"
    registry = {}
    plugin_synonym = ("reporter", "reporter")


class AbstractReporter:
    """
    Produces a meaningful report out of measured data.
    """

    def __init__(self, misc_settings = None, stats_helper: RunDataStatsHelper = None,
                 excluded_properties: t.List[str] = None):
        """
        Creates an instance.

        :param misc_settings: configuration
        :param stats_helper: used stats helper instance
        :param excluded_properties: measured properties that are excluded from the reports
        """
        FNumber.init_settings(Settings()["report/number"])
        excluded_properties = excluded_properties or Settings()["report/excluded_properties"]
        self.misc = misc_settings
        """ Configuration """
        self.stats_helper = None  # type: RunDataStatsHelper
        """ Used starts helper """
        if stats_helper is None:
            report_in = Settings()["report/in"]
            typecheck(report_in, Either(ValidYamlFileName(), ListOrTuple(ValidYamlFileName())))
            if isinstance(report_in, str):
                report_in = [report_in]
            runs = []
            for file in report_in:
                with open(file, "r") as f:
                    runs.extend(yaml.safe_load(f))
            self.stats_helper = RunDataStatsHelper.init_from_dicts(runs)
        else:
            self.stats_helper = stats_helper
        self.stats_helper = self.stats_helper.exclude_properties(excluded_properties)  # type: RunDataStatsHelper
        include_props = Settings()["stats/properties"]
        if "all" not in include_props:
            self.stats_helper = self.stats_helper.include_properties(include_props)
        self.stats_helper.make_descriptions_distinct()
        self.excluded_data_info = ExcludedInvalidData()  # type: ExcludedInvalidData
        if Settings()["report/exclude_invalid"]:
            self.stats_helper, self.excluded_data_info = self.stats_helper.exclude_invalid()
        self.to_long_prop_dict = {}
        """ Maps a property name to a long property name """
        if Settings()["report/long_properties"]:
            self.stats_helper, self.to_long_prop_dict = self.stats_helper.long_properties()
        self.stats = TestedPairsAndSingles(self.stats_helper.valid_runs())  # type: TestedPairsAndSingles
        """ This object is used to simplify the work with the data and the statistics """

    def report(self):
        """
        Create a report and output or store it as configured.
        """
        raise NotImplementedError()


@register(ReporterRegistry, "console", Dict({
    "out": FileNameOrStdOut() // Default("-") // Description("Output file name or `-` (stdout)"),
    "with_tester_results": Bool() // Default(True) // Description("Print statistical tests for every property for every"
                                                                  " two programs"),
    "mode": ExactEither("both", "cluster", "single", "auto") // Default("auto")
          // Description("'auto': report clusters (runs with the same description) and "
                         "singles (clusters with a single entry, combined) separately, "
                         "'single': report all clusters together as one, "
                         "'cluster': report all clusters separately, "
                         "'both': append the output of 'cluster' to the output of 'single'"),
    "report_errors": Bool() // Default(True) // Description("Report on the failing blocks"),
    "baseline": Str() // Default("") // Description("Matches the baseline block"),
    "baseline_position": ExactEither("each", "after", "both", "instead") // Default("each")
                    // Description("Position of the baseline comparison: each: after each block, after: after each "
                                   "cluster, both: after each and after cluster, instead: instead of the non baselined")
}))
class ConsoleReporter(AbstractReporter):
    """
    Simple reporter that outputs just text.
    """

    def report(self, with_tester_results: bool = True, to_string: bool = False) -> t.Optional[str]:
        """
        Create an report and output it as configured.

        :param with_tester_results: include the hypothesis tester results
        :param to_string: return the report as a string and don't output it?
        :return: the report string if ``to_string == True``
        """
        output = [""]

        with_tester_results = with_tester_results and self.misc["with_tester_results"]

        baselines = filter_runs(self.stats_helper.runs, self.misc["baseline"]) if self.misc["baseline"] else []

        def string_printer(line: str, **args):
            output[0] += str(line) + "\n"

        with click.open_file(self.misc["out"], mode='w') as f:
            print_func = string_printer if to_string else lambda x: print(x, file=f)
            if self.misc["mode"] == "auto":
                single, clusters = self.stats_helper.get_description_clusters_and_single()
                self._report_cluster("single runs", single, print_func, with_tester_results, baselines)
                self._report_clusters(clusters, print_func, with_tester_results, baselines)
            if self.misc["mode"] in ["both", "single"]:
                self._report_cluster("all runs",
                                     self.stats_helper.runs,
                                     print_func,
                                     with_tester_results,
                                     baselines)
            if self.misc["mode"] in ["both", "cluster"]:
                self._report_clusters(self.stats_helper.get_description_clusters(), print_func, with_tester_results,
                                      baselines)
                print_func("")
            if self.misc["report_errors"] and len(self.stats_helper.errorneous_runs) > 0:
                self._report_errors(self.stats_helper.errorneous_runs, print_func)
            chown(f)
        if to_string:
            return output[0]

    def _report_clusters(self, clusters: t.Dict[str, t.List[RunData]], print_func: t.Callable[[str], None],
                         with_tester_results: bool, baselines: t.List[RunData]):
        for n, c in clusters.items():
            self._report_cluster(n,
                                 c,
                                 print_func,
                                 with_tester_results,
                                 baselines)

    def _report_cluster(self, description: str, blocks: t.List[RunData], print_func: t.Callable[[str], None],
                        with_tester_results: bool, baselines: t.List[RunData]):
        if not blocks:
            return
        print_func("Report for {}".format(description))
        descr_size = max(len(prop) for block in blocks for prop in block.properties)
        self._report_blocks(blocks, print_func, baselines, descr_size)
        if with_tester_results:
            stats_helper = RunDataStatsHelper(blocks, self.stats_helper.tester,
                                              property_descriptions=self.stats_helper.property_descriptions)
            self._report_list("Equal program blocks",
                              stats_helper.get_evaluation(blocks=blocks, with_equal=True,
                                                          with_uncertain=False,
                                                          with_unequal=False),
                              print_func, descr_size)
            self._report_list("Unequal program blocks",
                              stats_helper.get_evaluation(blocks=blocks, with_equal=False,
                                                          with_uncertain=False,
                                                          with_unequal=True),
                              print_func, descr_size)
            self._report_list("Uncertain program blocks",
                              stats_helper.get_evaluation(blocks=blocks, with_equal=False,
                                                          with_uncertain=True,
                                                          with_unequal=False),
                              print_func, descr_size)

    def _report_blocks(self, blocks: t.List[RunData], print_func: t.Callable[[str], None],
                       baselines: t.List[RunData], descr_size: int):
        if self.misc["baseline_position"] != "instead":
            for block in blocks:
                assert isinstance(block, RunData)
                self._report_block(block, print_func, baselines, descr_size)
        if self.misc["baseline_position"] in ["after", "both", "instead"]:
            for baseline in baselines:
                if baseline == block:
                    continue
                self._report_block_with_baseline(block, print_func, baseline, descr_size)
                print_func("")

    def _report_block(self, block: RunData, print_func: t.Callable[[str], None],
                      baselines: t.List[RunData], descr_size: int):
        print_func("{descr:<20} ({num:>5} single benchmarks)"
                   .format(descr=block.description(), num=len(block.data[block.properties[0]])))
        for prop in sorted(block.properties):
            mean = np.mean(block[prop])
            std = np.std(block[prop])
            mean_str = str(FNumber(mean, abs_deviation=std))
            dev = "{:>5.5%}".format(std / mean) if mean != 0 else "{:>5.5}".format(std)
            print_func("\t {{prop:<{}}} mean = {{mean:>15s}}, deviation = {{dev:>11s}}".format(descr_size)
                .format(
                prop=prop, mean=mean_str,
                dev=dev))
        print_func("")
        if self.misc["baseline_position"] in ["each", "both", "instead"]:
            for baseline in baselines:
                if baseline == block:
                    continue
                self._report_block_with_baseline(block, print_func, baseline, descr_size)
                print_func("")

    def _report_block_with_baseline(self, block: RunData, print_func: t.Callable[[str], None], baseline: RunData,
                                    descr_size: int):
        print_func("{descr:<20} ({num:>5}) with baseline {descr2:<20} ({num2:>5})"
                   .format(descr=block.description(), num=len(block.data[block.properties[0]]),
                           descr2=block.description(), num2=len(block.data[block.properties[0]])))
        combined_props = set(block.properties) & set(baseline.properties)
        tester = TesterRegistry.get_tester()
        for prop in sorted(combined_props):
            mean = np.mean(block[prop])
            std = np.std(block[prop])
            base_mean = baseline.get_single_properties()[prop].mean()
            base_std = baseline.get_single_properties()[prop].std()
            mean_str = str(FNumber(mean / base_mean, abs_deviation=std / base_mean, is_percent=True))
            dev = "{:>5.5%}".format(std / mean) if mean != 0 else "{:>5.5}".format(std)
            print_func("\t {{prop:<{}}} mean = {{mean:>15s}}, confidence = {{conf:>5.0%}}, dev = {{dev:>11s}}, "
                       "{{dbase:>11s}}".format(descr_size)
                .format(
                    prop=prop,
                    mean=mean_str,
                    dev=dev,
                    conf=tester.test(block[prop], baseline[prop]),
                    dbase="{:>5.5%}".format(base_std / base_mean) if base_mean != 0 else "{:>5.5}".format(base_std)))
        rels = [(block.get_single_properties()[prop].mean() / baseline.get_single_properties()[prop].mean())
                            for prop in combined_props]
        gmean = stats.gmean(rels)
        gstd = util.geom_std(rels)
        print_func("geometric mean of relative mean = {:>15}, std dev = {:>15}"
                   .format(FNumber(gmean, is_percent=True).format(), FNumber(gstd, is_percent=True).format()))

    def _report_list(self, title: str, items: t.List[dict], print_func: t.Callable[[str], None], descr_size: int):
        if len(items) != 0:
            print_func(title)
        for item in items:
            print_func("\t {}  ⟷   {}".format(item["data"][0].description(),
                                       item["data"][1].description()))
            for prop in sorted(item["properties"]):
                prop_data = item["properties"][prop]
                perc = prop_data["p_val"]
                if prop_data["unequal"]:
                    perc = 1 - perc
                print_func("\t\t {{descr:<{}}} confidence = {{perc:>5.0%}}, speed up = {{speed_up:>10.2%}}"
                      .format(descr_size).format(descr=prop_data["description"], perc=perc,
                              speed_up=prop_data["speed_up"]))
            print_func("")

    def _report_errors(self, errorneous_runs: t.List[RunData], print_func: t.Callable[[str], None]):
        print_func("Errorneous runs")
        for run in errorneous_runs:
            print_func("""\t{d}:\n\t\t{m}""".format(d=run.description(), m="\n\t\t".join(str(run.recorded_error).split("\n"))))


@register(ReporterRegistry, "html", Dict(unknown_keys=True) // Default({}) // Description("Deprecated setting"),
          deprecated=True)
class HTMLReporter(AbstractReporter):
    """
    Deprecated reporter that just lives as a hull.
    It might be useful to revive it as a basic reporter without JavaScript.
    """

    def report(self):
        raise NotImplementedError("The html reporter is broken. Consider using the html2 reporter.")


@register(ReporterRegistry, "html2", Dict({
    "out": Str() // Default("report") // Description("Output directory"),
    "html_filename": Str() // Default("report.html") // Description("Name of the HTML file"),
    "fig_width_small": Float() // Default(15.0) // Description("Width of all small plotted figures"),
    "fig_width_big": Float() // Default(25.0) // Description("Width of all big plotted figures"),
    "boxplot_height": Float() // Default(2.0) // Description("Height per run block for the big comparison box plots"),
    "alpha": Float() // Default(0.05) // Description("Alpha value for confidence intervals"),
    "gen_tex": Bool() // Default(True) // Description("Generate simple latex versions of the plotted figures?"),
    "gen_pdf": Bool() // Default(False) // Description("Generate pdf versions of the plotted figures?"),
    "gen_xls": Bool() // Default(False) // Description("Generate excel files for all tables"),
    "show_zoomed_out": Bool() // Default(True)
                       // Description("Show zoomed out (x min = 0) figures in the extended summaries?"),
    "percent_format": Str() // Default("{:5.2%}") // Description("Format string used to format floats as percentages"),
    "float_format": Str() // Default("{:5.2e}") // Description("Format string used to format floats"),
    "min_in_comparison_tables": Bool() // Default(False)
                                // Description("Show the mininmum related values in the big comparison table"),
    "mean_in_comparison_tables": Bool() // Default(True)
                                // Description("Show the mean related values in the big comparison table"),
    "force_override": Bool() // Default(False)
                                // Description("Override the contents of the output directory if it already exists?"),
    "hide_stat_warnings": Bool() // Default(False)
                                // Description("Hide warnings and errors related to statistical properties"),
    "local": Bool() // Default(False) // Description("Use local versions of all third party resources")
}))
class HTMLReporter2(AbstractReporter):
    """
    Reporter that produces a HTML based report with lot's of graphics.
    A rewrite of the original HTMLReporter
    """

    _counter = 0
    """ Just a counter to allow collision free figure saving. """

    def report(self):
        import humanfriendly as hf
        typecheck(self.misc["out"], DirName(), value_name="reporter option out")
        start_time = time.time()
        if os.path.exists(self.misc["out"]):
            force = self.misc["force_override"]
            if not force:
                force = click.prompt("The output folder already exists. Should its contents be overridden? ", type=bool,
                                     default=False)
            if force:
                shutil.rmtree(self.misc["out"])
            else:
                return
        os.makedirs(self.misc["out"])
        if self.misc["local"]:
            for folder in ["js", "css"]:
                resources_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "report_resources/" + folder))
                shutil.copytree(resources_path, self.misc["out"] + "/" + folder)
        else:
            resources_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "report_resources"))
            os.makedirs(self.misc["out"] + "/js")
            os.makedirs(self.misc["out"] + "/css")
            shutil.copy(resources_path + "/js/script.js", self.misc["out"] + "/js")
            shutil.copy(resources_path + "/css/style.css", self.misc["out"] + "/css")
        for folder in ["js", "css"]:
            os.chmod(self.misc["out"] + "/" + folder, 0o755)
            chown(self.misc["out"] + "/" + folder)

        runs = self.stats_helper.valid_runs()
        self._percent_format = self.misc["percent_format"]
        self._float_format = self.misc["float_format"]
        self._hide_stat_warnings = self.misc["hide_stat_warnings"]
        self.zoom_in = not self.misc["show_zoomed_out"]

        deps = {
            "css/jquery.ui.all.css":
             "https://cdnjs.cloudflare.com/ajax/libs/jqueryui/1.9.1/themes/base/jquery.ui.all.min.css",
            "js/jquery.tocify.js":
             "https://cdnjs.cloudflare.com/ajax/libs/jquery.tocify/1.9.0/javascripts/jquery.tocify.min.js",
            "css/jquery.tocify.css":
             "https://cdnjs.cloudflare.com/ajax/libs/jquery.tocify/1.9.0/stylesheets/jquery.tocify.min.css",
            "js/bootstrap.min.js": "https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/3.4.1/js/bootstrap.min.js",
            "css/bootstrap.min.css": "https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/3.4.1/css/bootstrap.min.css",
            "js/jquery.min.js": "https://cdnjs.cloudflare.com/ajax/libs/jquery/1.12.4/jquery.min.js",
            "js/custom-mathjax.min.js": "https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.1/MathJax.js?config=TeX-AMS-MML_SVG",
            "js/jquery-ui-1.9.1.custom.min.js": "https://cdnjs.cloudflare.com/ajax/libs/jqueryui/1.9.1/jquery.ui.widget.min.js"
        }

        self._app_html = ""
        html = """<html lang="en">
    <head>
        <title>Benchmarking report</title>
        <meta charset="UTF-8"/>
        <link rel="stylesheet" src="css/jquery.ui.all.css">
        <link rel="stylesheet" src="css/jquery.tocify.css">
        <link rel="stylesheet" href="css/bootstrap.min.css">
        <link rel="stylesheet" href="css/style.css">
        <script src="js/jquery.min.js"></script>
        <script src="js/bootstrap.min.js"></script>
        <script src="js/jquery-ui-1.9.1.custom.min.js"></script>
        <script src="js/script.js"></script>
    </head>
    <body style="font-family: sans-serif;">
        <a href="" id="hidden_link" style="display: none;"></a>
        <div id="toc"></div>
        <div class="container">
          <div class="row">
             <div class="col-sm-3">
                <div id="toc"></div>
            </div>
             <!-- sidebar, which will move to the top on a small screen -->
             <!-- main content area -->
             <div class="col-sm-9">
                <div class="page-header">
                    <h1>Benchmarking report</h1>
                    <p class="lead">A benchmarking report comparing {comparing_str}</p>
                  </div>
                {inner_html}
                <footer class="footer">Generated by temci in {timespan}</footer>
             </div>
          </div>
        </div>
        {self._app_html}
        <script src="js/custom-mathjax.min.js"></script>
        <script>
            $(function () {{
                $('[data-toggle="popover"]').popover({{"content": function(){{
                    return $("#" + this.getAttribute("data-content-id")).html();
                }}}})
                $('body').on('click', function (e) {{
                    $('[data-toggle="popover"]').each(function () {{
                        //the 'is' for buttons that trigger popups
                        //the 'has' for icons within a button that triggers a popup
                        if (!$(this).is(e.target) && $(this).has(e.target).length === 0 && $('.popover').has(e.target).length === 0) {{
                            $(this).popover('hide');
                        }}
                    }});
                }});
            }})
        </script>
    </body>
</html>
        """
        if not self.misc["local"]:
            for dep, repl in deps.items():
                html = html.replace(dep, repl)
        if self.misc["gen_pdf"] and not util.has_pdflatex():
            util.warn_for_pdflatex_non_existence_once()
            self.misc["gen_pdf"] = False
        comparing_str = join_strs([single.description() for single in self.stats.singles])
        inner_html = self._format_excluded_data_warnings()
        inner_html += """
            <h2>Summary</h2>
        """
        inner_html += self._format_errors_and_warnings(self.stats)
        if len(self.stats.properties()) > 1:
            inner_html += """
                <h3>Overall summary</h3>
            """
            inner_html += self._full_single_property_comp_table().html()
        for prop in self.stats.properties():
            inner_html += """
                <h3>Summary regarding {prop}</h3>
            """.format(**locals())
            inner_html += self._full_single_property_comp_table(prop).html()
            inner_html += """
                <p/>
            """
            inner_html += self._comparison_for_prop(prop)

        for single in self.stats.singles:
            inner_html += """<div class="block">"""
            inner_html += self._extended_summary(single, with_title=True, title_level=2,
                                                 title_class="page-header") + """</div>"""
        for pair in self.stats.pairs:
            inner_html += """<div class="block">"""
            inner_html += self._extended_summary(pair, with_title=True, title_level=2,
                                                 title_class="page-header") + """</div>"""
        inner_html += self._format_hw_info()
        self._write(html.format(timespan=hf.format_timespan(time.time() - start_time), **locals()))
        logging.info("Finished generating html")
        logging.info("Generate images...")
        self._process_hist_cache(self._hist_async_img_cache.values(), "Generate images")
        self._process_boxplot_cache(self._boxplot_async_cache.values(), "Generate box plots")
        self._write(html.format(timespan=hf.format_timespan(time.time() - start_time), **locals()))
        if self.misc["gen_pdf"] or self.misc["gen_tex"]:
            strs = (["tex"] if self.misc["gen_tex"] else []) + (["pdf"] if self.misc["gen_pdf"] else [])
            self._process_hist_cache(self._hist_async_misc_cache.values(), "Generate {}".format(join_strs(strs)))

    def _format_float(self, val: float) -> str:
        return self._float_format.format(val)

    def _process_hist_cache(self, cache: t.Iterable[dict], title: str):
        pool = multiprocessing.Pool(4)
        pool_res = [pool.apply_async(self._process_hist_cache_entry, args=(entry,)) for entry in cache]
        if Settings().has_log_level("info"):
            with click.progressbar(pool_res, label=title) as pool_res:
                for res in pool_res:
                    res.get()
        else:
            for res in pool_res:
                res.get()

    def _process_boxplot_cache(self, cache: t.Iterable[dict], title: str):
        pool = multiprocessing.Pool(4)
        pool_res = [pool.apply_async(self._process_boxplot_cache_entry, args=(entry,)) for entry in cache]
        if Settings().has_log_level("info"):
            with click.progressbar(pool_res, label=title) as pool_res:
                for res in pool_res:
                    res.get()
        else:
            for res in pool_res:
                res.get()

    def _write(self, html_string: str):
        """
        Store the html string in the appropriate file and append "</center></body></html>"
        """
        report_filename = os.path.join(self.misc["out"], self.misc["html_filename"])
        with open(report_filename, "w") as f:
            f.write(html_string)
            logging.info("Wrote report into " + report_filename)
            chown(f)

    def _format_excluded_data_warnings(self):
        html = ""
        if self.excluded_data_info.excluded_run_datas:
            html += """
                <div class="alert alert-danger">
                     Excluded invalid {} (the data consists only of zeroes or NaNs).
                </div>
            """.format(join_strs(map(repr, self.excluded_data_info.excluded_run_datas)))
        for descr in self.excluded_data_info.excluded_properties_per_run_data.keys():
            html += """
                <div class="alert alert-warning">
                  Excluded {} from {!r} (the data consists only of zeroes or NaNs).
                </div>
            """.format(join_strs(map(repr, self.excluded_data_info.excluded_properties_per_run_data[descr])),
                       descr)
        return html

    def _format_hw_info(self) -> str:
        def format_hw_info_section(name: str, content: t.List[t.Tuple[str, str]]) -> str:
            return """<h3>{}</h3>
            <table class="table">{}</table>"""\
                .format(name, "\n".join("<tr><th>{}</th><td>{}</td></tr>".format(n[0].upper() + n[1:], v) for n, v in content))
        if self.stats_helper.env_info:
            return """<h2 id="env_info" class="page-header">Environment info</h2>
            {}
            """.format("\n".join(format_hw_info_section(name, content) for name, content in self.stats_helper.env_info))
        return ""

    def _full_single_property_comp_table(self, property: str = None) -> '_Table':
        header_cells = []
        for single in self.stats.singles:
            _single = SingleProperty(single, single.rundata, property) if property is not None else single
            modal_id = self._short_summary_modal(_single)
            header_cells.append(_Cell(self, content=self._obj_description(single), color_class_obj=single,
                                      modal_id=modal_id))
        table = _Table(self, header_cells, header_cells, _Cell(self, "vs."))

        for i in range(self.stats.number_of_singles()):
            for j in range(self.stats.number_of_singles()):
                if i == j:
                    table[i, j] = _Cell(self)
                    continue
                popover = _Popover(self, "Explanation", content="")
                cell = None
                pair = self.stats.get_pair(i, j)
                rel_diff = None
                if property is None:
                    popover.content = r"""
                        Geometric mean of the left means relative to the right means:
                        \[\sqrt[\|properties\|]{
                        \prod_{p \in \text{properties}}
                        \frac{\overline{\text{left[p]}}}{
                            \overline{\text{right[p]}}}}\]
                        <p>Using the more widely known arithmetic mean would be like
                        <a href='http://ece.uprm.edu/~nayda/Courses/Icom6115F06/Papers/paper4.pdf?origin=publication_detail'>
                        lying</a>.</p>
                        <p>The geometric standard deviation is <b>%s</b></p>
                    """ % self._percent_format.format(pair.first_rel_to_second_std())
                    rel_diff = fnumber(pair.first_rel_to_second(), rel_deviation=pair.first_rel_to_second_std() - 1, is_percent=True)
                    popover.trigger = "hover click"
                else:
                    pair = pair[property]
                    popover.content = """Left mean relative to the right mean:
                    \\begin{{align}}
                        & \\frac{{\\overline{{\\text{{left[{}]}}}}}}{{\\overline{{\\text{{right[{}]}}}}}} \\\\
                        &= \\frac{{{:5.4f}}}{{{:5.4f}}}
                    \\end{{align}}
                    <p>The maximum standard deviation of both benchmarks relative to the mean of the right one is <b>{}</b>.</p>
                    """.format(property, property, pair.first.mean(), pair.second.mean(),
                           self._percent_format.format(pair.max_std_dev() / pair.second.mean()))
                    rel_diff = FNumber(pair.first_rel_to_second(), rel_deviation=pair.max_std_dev() / pair.second.mean(), is_percent=True)
                cell = _Cell(self, content=str(rel_diff), popover=popover, color_class_obj=pair, show_click_on_info=True)
                cell.modal_id = self._short_summary_modal(pair)
                table[i, j] = cell
        return table

    def _extended_summary(self, obj: BaseStatObject, with_title: bool = True, title_level: int = 3,
                          title_class: str = "") -> str:
        html = ""
        other_id_obj = None # type: BaseStatObject
        if isinstance(obj, Single):
            html += self._extended_summary_of_single(obj, title_level)
        if isinstance(obj, SingleProperty):
            html += self._extended_summary_of_single_property(obj, title_level)
        if isinstance(obj, TestedPair):
            html += self._extended_summary_of_tested_pair(obj, title_level)
            other_id_obj = obj.swap()
        if isinstance(obj, TestedPairProperty):
            html += self._extended_summary_of_tested_pair_property(obj, title_level)
        if with_title:
            other_id_app = "" if other_id_obj is None else """<div id="{}"/>"""\
                .format(self._html_id_for_object("misc", other_id_obj))
            html = """<h{level} id='{id}' class="{tc}">
                            {title}</h{level}>""".format(level=title_level, tc=title_class,
                                                         title=self._obj_description(obj),
                                                         id=self._html_id_for_object("misc", obj)) + other_id_app + html
        return html

    def _extended_summary_of_single(self, obj: Single, title_level: int) -> str:
        html = self._short_summary(obj, use_modals=True, extended=False, title_level=title_level + 1)
        for prop in sorted(obj.properties.keys()):
            html += """<div class="sub-block"><h{level} class="page-header" id="{id}">{prop}</h{level}>""".format(
                level=title_level + 1, prop=prop, id=self._html_id_for_object("misc", obj.properties[prop])
            )
            html += self._extended_summary(obj.properties[prop], with_title=False,
                                           title_level=title_level + 1, title_class="page-header")
            html += """</div>"""
        return html

    def _extended_summary_of_single_property(self, obj: SingleProperty, title_level: int) -> str:
        html = self._short_summary(obj, use_modals=True, extended=True, title_level=title_level + 1)
        return html

    def _extended_summary_of_tested_pair(self, obj: TestedPair, title_level: int) -> str:
        html = self._short_summary(obj, use_modals=True, extended=True, title_level=title_level + 1)
        swapped = obj.swap()
        for prop in sorted(obj.properties.keys()):
            html += """
                <div class="sub-block">
                    <h{level} class="page-header" id="{id}">{prop}</h{level}>
                    <div id="{id2}"></div>""".format(
                level=title_level + 1, prop=prop, id=self._html_id_for_object("misc", obj.properties[prop]),
                id2=self._html_id_for_object("misc", swapped.properties[prop])
            )
            html += self._extended_summary(obj.properties[prop], with_title=False,
                                           title_level=title_level + 1, title_class="page-header")
            html += """</div>"""
        return html


    def _extended_summary_of_tested_pair_property(self, obj: TestedPairProperty, title_level: int) -> str:
        html = self._short_summary(obj, use_modals=True, extended=True, title_level=title_level + 1)
        return html

    def _short_summary(self, obj: BaseStatObject, with_title: bool = False, title_level: int = 4,
                       use_modals: bool = False, extended: bool = False) -> str:
        html = ""
        if with_title:
            html += "<h{level}>{title}</h{level}>".format(level=title_level, title=self._obj_description(obj))
        html += self._format_errors_and_warnings(obj)
        if isinstance(obj, SingleProperty):
            html += self._short_summary_of_single_property(obj, use_modals, extended)
        if isinstance(obj, TestedPairProperty):
            html += self._short_summary_of_tested_pair_property(obj, use_modals, extended)
        if isinstance(obj, TestedPair):
            html += self._short_summary_of_tested_pair(obj, use_modals, extended)
        if isinstance(obj, Single):
            html += self._short_summary_of_single(obj, use_modals, extended)
        return html

    def _short_summary_of_single(self, obj: Single, use_modal: bool = False, extended: bool = False):
        obj_descrs = sorted(obj.properties.keys())
        objs = [obj.properties[val] for val in obj_descrs]
        return self._short_summary_table_for_single_property(objs=objs, objs_in_cols=False,
                                                             obj_descrs=obj_descrs, use_modal=use_modal,
                                                             extended=extended)

    def _short_summary_of_single_property(self, obj: SingleProperty, use_modals: bool = False, extended: bool = False):
        filenames = self._histogram(obj, big=extended, zoom_in=self.zoom_in)
        html = self._filenames_to_img_html(filenames)
        #if extended and not self.zoom_in:
        #    html += self._filenames_to_img_html(self._histogram(obj, big=extended, zoom_in=False))
        html += self._short_summary_table_for_single_property([obj], objs_in_cols=True, use_modal=use_modals,
                                                              extended=extended)
        return html

    def _short_summary_of_tested_pair_property(self, obj: TestedPairProperty, extended: bool = False,
                                               use_modals: bool = False):
        filenames = self._histogram(obj, big=extended, zoom_in=self.zoom_in)
        html = self._filenames_to_img_html(filenames)
        # if extended and self.misc["show_zoomed_out"]:
        #     filenames = self._histogram(obj, big=extended, zoom_in=self.zoom_in)
        #     html += self._filenames_to_img_html(filenames)
        ci_popover = _Popover(self, "Confidence interval", """
                        The chance is \\[ 1 - \\alpha = {p} \\] that the mean difference
                        \\begin{{align}} &\\text{{{first}}} - \\text{{{second}}} \\\\ =& {diff} \\end{{align}}
                        lies in the interval \\(({ci[0]:5.5f}, {ci[1]:5.5f})\\) (assuming the data is normal
                        distributed to a certain degree).
                        """.format(p=1-self.misc["alpha"], first=str(obj.first.parent),
                                   second=str(obj.second.parent), prop=obj.property,
                                   diff=obj.mean_diff(), ci=obj.mean_diff_ci(self.misc["alpha"])))
        tested_per_prop = [
            {
                "title": "Difference of means",
                "popover": _Popover(self, "Explanation", """
                    Difference between the mean of the left and the mean of the right.
                    It's the absolute difference and is often less important that the relative differences.
                """),
                "func": lambda x: fnumber(x.mean_diff(), abs_deviation=x.max_std_dev()),
                "format": self._float_format
            }, {
                "title": "... per left mean",
                "func": lambda x: fnumber(x.mean_diff_per_mean(), #rel_deviation=x.max_rel_std_dev(),
                                          is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """The mean difference relative to the left mean
                \\begin{align}
                    & \\frac{ \\overline{\\text{%s}} - \\overline{\\text{%s}}}{ \\overline{\\text{%s}} } \\\\
                    &= \\frac{ %f }{ %f}
                \\end{align}
                gives a number that helps to talk about the practical significance of the mean difference.
                """ % (obj.first.parent.description(), obj.second.parent.description(), str(obj.first.parent),
                       float(obj.mean_diff()), float(obj.first.mean())))
            }, {
                "title": "... per max std dev",
                "func": lambda x: fnumber(x.mean_diff_per_dev(), #rel_deviation=x.max_rel_std_dev(),
                                          is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """
                    The mean difference relative to the maximum standard deviation:
                    \\begin{{align}}
                        &\\frac{{
                            \\overline{{
                                \\text{{{first}}}
                                }}
                             - \\overline{{\\text{{{second}}}}}}}{{
                     \\text{{max}}(\\sigma_\\text{{{first}}}, \\sigma_\\text{{{second}}}) }} \\\\
                        = &  \\frac{{{md}}}{{{std}}}  \\end{{align}}
                    {context}
                """.format(first=obj.first.parent.description(), second=obj.second.parent.description(),
                           md=obj.mean_diff(), std=obj.max_std_dev(),
                           context=""  if self._hide_stat_warnings else """It's important because, as <a href='http://www.cse.unsw.edu.au/~cs9242/15/lectures/05-perfx4.pdf'>
                    Gernot Heiser</a> points out:
                    <ul>
                        <li>Don't believe any effect that is less than a standard deviation</li>
                        <li>Be highly suspicious if it is less than two standard deviations</li>
                    </ul>"""), trigger="hover click",)
            }, {
                "title": "... ci (lower bound)",
                "func": lambda x: fnumber(x.mean_diff_ci(self.misc["alpha"])[0]),
                "format": self._float_format,
                "extended": True,
                "popover": ci_popover
            } ,{
                "title": "... ci (upper bound)",
                "func": lambda x: fnumber(x.mean_diff_ci(self.misc["alpha"])[1]),
                "format": self._float_format,
                "extended": True,
                "popover": ci_popover
            }, {
                "title": obj.tester.name,
                "func": lambda x: x.equal_prob(),
                "format": self._percent_format,
                "popover": self._popover_for_tester(obj.tester)
            }, {
                "title": "min n",
                "func": lambda x: x.min_observations(),
                "format": "{}",
                "popover": _Popover(self, "Explanation", """
                    The minimum of the number of valid runs of both.
                or statistically spoken: the minimum sample size.""")
            }
        ]
        if not extended:
            l = []
            for elem in tested_per_prop:
                if not ("extended" in elem and elem["extended"]):
                    l.append(elem)
            tested_per_prop = l

        def content_func(row_header: str, col_header: str, row: int, col: int):
            res = tested_per_prop[row]["func"](obj)
            if isinstance(res, str):
                return res
            return tested_per_prop[row]["format"].format(res)

        def header_popover_func(elem, index: int, is_header_row: bool):
            if not is_header_row and "popover" in tested_per_prop[index]:
                return tested_per_prop[index]["popover"]

        table = _Table.from_content_func(self, cols=[obj],
                                         rows=list(map(lambda d: d["title"], tested_per_prop)),
                                         content_func=content_func, anchor_cell=_Cell(self),
                                         header_popover_func=header_popover_func)
        html += str(table)
        html += self._short_summary_table_for_single_property(objs=[obj.first, obj.second],
                                                              obj_descrs=[obj.first.description(),
                                                                          obj.second.description()],
                                                              objs_in_cols=False,
                                                              use_modal=use_modals)
        return html

    def _short_summary_of_tested_pair(self, obj: TestedPair, extended: bool = False, use_modals: bool = False) -> str:
        ts = None # type: TestedPairProperty
        tested_per_prop = []
        if self.misc["mean_in_comparison_tables"]:
            tested_per_prop.extend([
            {
                "title": "Difference of means",
                "popover": _Popover(self, "Explanation", """
                    Difference between the mean of the left and the mean of the right.
                    It's the absolute difference and is often less important that the relative differences.
                """),
                "func": lambda x: fnumber(x.mean_diff(), abs_deviation=x.max_std_dev()),
                "format": self._float_format
            }, {
                "title": "... per left mean",
                "func": lambda x: fnumber(x.mean_diff_per_mean(), abs_deviation=x.max_std_dev() / x.first.mean(),
                                          is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """The mean difference relative to the left mean
                gives a number that helps to talk about the practical significance of the mean difference.
                A tiny difference might be cool, but irrelevant (as caching effects are probably higher, use the
                \\verb|temci build| if you are curious about this).
                """)
            }, {
                "title": "... per max std dev",
                "func": lambda x: fnumber(x.mean_diff_per_dev(), rel_deviation=x.max_std_dev() / x.first.mean(),
                                          is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """
                The mean difference relative to the maximum standard deviation is important,
                because it puts the value info context{context}
                """.format(context="." if self._hide_stat_warnings else """, or as <a href='http://www.cse.unsw.edu.au/~cs9242/15/lectures/05-perfx4.pdf'>
                    Gernot Heiser</a> points out:
                    <ul>
                        <li>Don't believe any effect that is less than a standard deviation</li>
                        <li>Be highly suspicious if it is less than two standard deviations</li>
                    </ul>"""), trigger="hover click")
            }])

        if self.misc["min_in_comparison_tables"]:
            tested_per_prop.extend([{
                "title": "Difference of mins",
                "popover": None,#Popover(self, "", """  """),
                "func": lambda x: fnumber(x.first.min() - x.second.min()),
                "format": self._float_format
            }, {
                "title": "... per left min",
                "func": lambda x: fnumber((x.first.min() - x.second.min()) / x.first.min(), is_percent=True),
                "format": self._percent_format,
                "popover": None,#Popover(self, "Explanation", """            """)
            }, {
                "title": "... per max std dev",
                "func": lambda x: fnumber((x.first.min() - x.second.min()) / x.max_std_dev(), is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """
                The mean difference relative to the maximum standard deviation is important,
                because as <a href='http://www.cse.unsw.edu.au/~cs9242/15/lectures/05-perfx4.pdf'>
                    Gernot Heiser</a> points out:
                    <ul>
                        <li>Don't believe any effect that is less than a standard deviation</li>
                        <li>Be highly suspicious if it is less than two standard deviations</li>
                    </ul>
                """, trigger="hover click")
            }])
#        if self.misc["mean_in_comparison_tables"]:
#            tested_per_prop.extend([{
#               "title": "... ci",
#               "func": lambda x: x.mean_diff_ci(self.misc["alpha"])[0],
#               "format": self._float_format,
#               "extended": True,
#               "popover": Popover(self, "Confidence interval", """
#                       The chance is \\[ 1 - \\alpha = {p} \\] that the mean difference
#                       lies in the interval of which the lower and the upper bound are given
#                       (assuming the data is normal distributed to a certain degree).
#                                               """.format(p=1-self.misc["alpha"]))
#           } ,{
#               "title": "",
#               "func": lambda x: x.mean_diff_ci(self.misc["alpha"])[1],
#               "format": self._float_format,
#               "extended": True,
#               "popover": Popover(self, "Confidence interval", """
#                       The chance is \\[ 1 - \\alpha = {p} \\] that the mean difference
#                       lies in the interval of which the lower and the upper bound are given.
#                                               """.format(p=1-self.misc["alpha"]))
#           }])
        tested_per_prop.extend([{
                "title": obj.tester.name + " test",
                "func": lambda x: x.equal_prob(),
                "format": self._percent_format,
                "popover": self._popover_for_tester(obj.tester)
            }])

        if not extended:
            l = []
            for elem in tested_per_prop:
                if not ("extended" in elem and elem["extended"]):
                    l.append(elem)
            tested_per_prop = l

        def header_link_func(elem: str, index: int, is_header_row: bool):
            if not is_header_row and not use_modals:
                return "#" + self._html_id_for_object("misc", obj.properties[elem])

        def header_modal_func(elem: str, index: int, is_header_row: bool):
            if not is_header_row and use_modals:
                return self._short_summary_modal(obj.properties[elem])

        def content_func(row_header: str, col_header: str, row: int, col: int):
            d = tested_per_prop[col]
            res = d["func"](obj.properties[row_header])
            if isinstance(res, str):
                return res
            return d["format"].format(res)

        def header_color_obj(elem, index: int, is_header_row: bool):
            if not is_header_row:
                return obj[elem]

        def header_popover_func(elem, index: int, is_header_row: bool):
            if is_header_row and "popover" in tested_per_prop[index]:
                return tested_per_prop[index]["popover"]

        table = _Table.from_content_func(self, rows=sorted(list(obj.properties.keys())),
                                         cols=list(map(lambda d: d["title"], tested_per_prop)),
                                         header_link_func=header_link_func,
                                         content_func=content_func, anchor_cell=_Cell(self),
                                         header_color_obj_func=header_color_obj,
                                         header_modal_func=header_modal_func,
                                         header_popover_func=header_popover_func)
        html = str(table)
        html += """
            <p {po}>The geometric mean of the values of the properties of {first} relative to the values of 
            {second} is <b>{rel_diff}</b>
            (geometric standard deviation is {std})</p>
        """.format(po=_Popover(self, "Explanation", """
                        Geometric mean of the means of the left relative to the means of the right:
                        \\[\\sqrt[\\|properties\\|]{
                        \\prod_{p \\in \\text{properties}}
                        \\frac{\\overline{\\text{left[p]}}}{
                            \\overline{\\text{right[p]}}}}\\]
                        Using the more widely known would be like
                        <a href='http://ece.uprm.edu/~nayda/Courses/Icom6115F06/Papers/paper4.pdf?origin=publication_detail'>
                        lying</a>.
                 """, trigger="hover click"), first=obj.first, second=obj.second, rel_diff=self._format_float(obj.first_rel_to_second()),
                   std=self._format_float(obj.first_rel_to_second_std()))
        return html

    def _short_summary_table_for_single_property(self, objs: t.List[SingleProperty], use_modal: bool,
                                                 objs_in_cols: bool, obj_descrs: t.List[str] = None,
                                                 extended: bool = False) -> str:
        """
        :param objs: objects to look on
        :param use_modal: use modals for meta information, not simple links?
        :param objs_in_cols: show the different objects in own columns, not rows
        :param extended: more infos
        :return:
        """
        obj_descrs = obj_descrs or [self._obj_description(obj) for obj in objs]

        #objs[0]..std_dev_per_mean()
        mean_ci_popover = _Popover(self, "Mean confidence interval", """
                The chance is \\[ 1 - \\alpha = {p} \\] that the mean lies in the given interval
                (assuming the data is normal distributed to a certain degree).
                """.format(p=1-self.misc["alpha"]))
        std_dev_ci_popover = _Popover(self, "Standard deviation confidence interval", """
                The chance is \\[ 1 - \\alpha = {p} \\] that the standard deviation lies in the given interval
                (assuming the data is normal distributed to a certain degree).
                """.format(p=1-self.misc["alpha"]))
        tested_per_prop = [
            {
                "title": "mean",
                "func": lambda x: fnumber(x.mean(), abs_deviation=x.std_dev()),
                "format": self._float_format,
                "popover": _Popover(self, "Explanation", """
                    The simple arithmetical mean
                    \\[ \\frac{1}{n}\\sum_{i=1}^{n} a_i. \\]
                """)
            }, {
                "title": "std dev",
                "popover": _Popover(self, "Explanation", """
                    The sample standard deviation
                    \\[ \\sigma_N = \\sqrt{\\frac{1}{N} \\sum_{i=1}^N (x_i - \\overline{x})^2} \\]
                    In statistics, the standard deviation is a measure that is used to quantify the amount of
                    variation or dispersion of a set of data values. A standard deviation close to 0
                    indicates that the data points tend to be very close to the mean (also called the
                    expected value) of the set, while a high standard deviation indicates that the data
                    points are spread out over a wider range of values.
                    (<a href='https://en.wikipedia.org/wiki/Standard_deviation'>wikipedia</a>)
                """, trigger="hover click"),
                "func": lambda x: fnumber(x.std_dev(), abs_deviation=x.sem()),
                "format": self._float_format,
                "extended": True
            }, {
                "title": r"\(\sigma\) per mean",
                "func": lambda x: fnumber(x.std_dev_per_mean(), rel_deviation=x.sem() / (x.mean() ** 2),
                                          is_percent=True),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """
                    The standard deviation relative to the mean is a measure of how big the relative variation
                    of data is. A small value is considered neccessary for a benchmark to be useful.
                    Or to quote <a href='https://www.cse.unsw.edu.au/~gernot/benchmarking-crimes.html'>
                    Gernot Heiser</a>:
                    <p>Always do several runs, and check the standard deviation. Watch out for abnormal variance.
                    In the sort of measurements we do, standard deviations are normally
                    expected to be less than 0.1%. If you see >1% this should ring alarm bells.</p>
                """, trigger="hover click")
            }, {
                "title": "sem",
                "popover": _Popover(self, "Explanation", """Standard error mean:
                    \\[ \\sigma(\\overline{X}) = \\frac{\\sigma}{\\sqrt{n}} \\]
                    <p>Put simply, the standard error of the sample is an estimate of how far the sample mean is
                    likely to be from the population mean, whereas the standard deviation of the sample is the
                    degree to which individuals within the sample differ from the sample mean.
                    (<a href='https://en.wikipedia.org/wiki/Standard_error'>wikipedia</a>)</p>""",
                                    trigger="hover focus"),
                "func": lambda x: fnumber(x.sem(), abs_deviation=x.sem() / math.sqrt(x.observations())),
                "format": self._float_format,
                "extended": False
            }, {
                "title": "median",
                "func": lambda x: fnumber(x.median(), abs_deviation=x.std_dev()),
                "format": self._float_format,
                "popover": _Popover(self, "Explanation", """
                    The median is the value that seperates that data into two equal sizes subsets
                    (with the &lt; and the &gt; relation respectively).
                    As the mean and the standard deviation are already given here, the median isn't important.
                """),
                "extended": True
            }, {
                "title": "min",
                "func": lambda x: fnumber(x.min()),
                "format": self._float_format,
                "popover": _Popover(self, "Explanation", """The minimum value. It's a bad sign if the maximum
                                                  is far lower than the mean and you can't explain it.
                                                  """),
                "extended": False
            }]
        if self.misc["min_in_comparison_tables"]:
            tested_per_prop.extend([{
                    "title": r"\(\sigma\) per min",
                    "func": lambda x: x.std_dev() / x.min(),
                    "format": self._float_format,
                    "popover": _Popover(self, "sdf", "sdf"),
                    "extended": False
                }])
        tested_per_prop.extend([{
                "title": "max",
                "func": lambda x: fnumber(x.max()),
                "format": self._float_format,
                "popover": _Popover(self, "Explanation", """The maximum value. It's a bad sign if the maximum
                                                  is far higher than the mean and you can't explain it.
                                                  """),
                "extended": True
            }, {
                "title": "n",
                "func": lambda x: x.observations(),
                "format": "{}",
                "popover": _Popover(self, "Explanation", """The number of valid runs
                or statistically spoken: the sample size."""),
                "extended": False
            }, {
                "title": "mean ci (lower bound)",
                "func": lambda x: fnumber(x.mean_ci(self.misc["alpha"])[0]),
                "format": self._float_format,
                "extended": True,
                "popover": mean_ci_popover
            } ,{
                "title": "mean ci (upper bound)",
                "func": lambda x: fnumber(x.mean_ci(self.misc["alpha"])[1]),
                "format": self._float_format,
                "extended": True,
                "popover": mean_ci_popover
            }, {
                "title": "std dev ci (lower bound)",
                "func": lambda x: fnumber(x.std_dev_ci(self.misc["alpha"])[0]),
                "format": self._float_format,
                "extended": True,
                "popover": mean_ci_popover
            } ,{
                "title": "std dev ci (upper bound)",
                "func": lambda x: fnumber(x.std_dev_ci(self.misc["alpha"])[1]),
                "format": self._float_format,
                "extended": True,
                "popover": mean_ci_popover
            }, {
                "title": "normality probability",
                "func": lambda x: x.normality(),
                "format": self._percent_format,
                "popover": _Popover(self, "Explanation", """
                    Quoting the
                    <a href='http://blog.minitab.com/blog/michelle-paret/using-the-mean-its-not-always-a-slam-dunk'>
                    minitab blog</a>:
                    <p>If process knowledge tells you that your data should follow a normal distribution,
                    then run a normality test to be sure. If your Anderson-Darling Normality
                    Test p-value is larger than, say, an alpha level of 0.05 (here {alpha}), then you can conclude
                    that your data follow a normal distribution and, therefore, the mean is an adequate
                    measure of central tendency.</p>
                    The T test is robust against non normality, but that's not the case fpr statistical properties like
                    the given confidence intervals.
                """.format(alpha=self.misc["alpha"])),
                "extended": True
            }
        ])

        if not extended:
            l = []
            for elem in tested_per_prop:
                if not ("extended" in elem and elem["extended"]):
                    l.append(elem)
            tested_per_prop = l

        def header_link_func(elem: SingleProperty, index: int, is_header_row: bool):
            if objs_in_cols == is_header_row and not use_modal:
                return "#" + self._html_id_for_object("misc", elem)

        def header_modal_func(elem: SingleProperty, index: int, is_header_row: bool):
            if objs_in_cols == is_header_row and use_modal:
                return self._short_summary_modal(elem)

        def header_popover_func(elem, index: int, is_header_row: bool):
            if objs_in_cols != is_header_row and "popover" in tested_per_prop[index]:
                return tested_per_prop[index]["popover"]

        def content_func(row_header: t.Union[SingleProperty, str], col_header: t.Union[SingleProperty, str],
                         row: int, col: int):
            d = {}
            obj = None # type: SingleProperty
            if objs_in_cols:
                d = tested_per_prop[row]
                obj = col_header
            else:
                d = tested_per_prop[col]
                obj = row_header
            res = d["func"](obj)
            if isinstance(res, str):
                return res
            return d["format"].format(res)

        def header_color_obj(elem, index: int, is_header_row: bool):
            if objs_in_cols == is_header_row:
                return elem

        def header_content_func(elem, index: int, is_header_row: bool) -> str:
            if objs_in_cols == is_header_row:
                return obj_descrs[index]
            return tested_per_prop[index]["title"]

        func_titles = list(map(lambda d: d["title"], tested_per_prop))
        rows = []
        cols = []
        if objs_in_cols:
            cols = objs
            rows = func_titles
        else:
            cols = func_titles
            rows = objs
        table = _Table.from_content_func(self, rows=rows,
                                         cols=cols,
                                         header_link_func=header_link_func,
                                         content_func=content_func, anchor_cell=_Cell(self),
                                         header_color_obj_func=header_color_obj,
                                         header_content_func=header_content_func,
                                         header_modal_func=header_modal_func,
                                         header_popover_func=header_popover_func)
        return str(table)

    def _comparison_for_prop(self, property) -> str:
        html = self._filenames_to_img_html(
            self._singles_property_boxplot(self.stats.singles_properties[property], big=True), kind="boxplot"
        )
        html += "<p/>"
        html += self._tabular_comparison_for_prop(property)
        return html

    def _tabular_comparison_for_prop(self, property: str) -> str:
        return self._short_summary_table_for_single_property(self.stats.singles_properties[property].singles,
                                                             use_modal=True, objs_in_cols=False)

    def _filenames_to_img_html(self, filenames: t.Dict[str, str], kind: str = "hist"):
        return """
            <center>
                <div {popover}>
                    <img width="100%" src="{img}" class="img"></img>
                </div>
            </center>
        """.format(popover=self._img_filenames_popover(filenames, kind),
                   img=self._filename_relative_to_out_dir(filenames["img"]))

    def _img_filenames_popover(self, filenames: t.Dict[str, str], kind: str = "hist") -> '_Popover':
        _filenames = {}
        for key in filenames:
            _filenames[key] = self._filename_relative_to_out_dir(filenames[key])
        filenames = _filenames
        html = """
            <div class='list-group'>
        """
        if "img" in filenames:
            html += """
                <a href='{img}' class='list-group-item'>
                    The current image
                </a>
            """.format(**filenames)
        if "pdf" in filenames:
            html += """
                <a href='{pdf}' class='list-group-item'>
                    PDF (generated by matplotlib)
                </a>
            """.format(**filenames)
        if "tex" in filenames:
            if kind == "hist":
                html += """
                    <a href='{tex}' class='list-group-item'>
                        TeX (requiring the package <code>pgfplots</code>)
                    </a>
                """.format(**filenames)
            elif kind == "boxplot":
                html += """
                    <a href='{tex}' class='list-group-item'>
                        TeX (requiring the package <code>pgfplots</code> and
                        <small><code>\\usepgfplotslibrary{{statistics}}</code></small>)
                    </a>
                """.format(**filenames)
            html +="""
                <a href='{tex_standalone}' class='list-group-item'>
                    Standalone TeX
                </a>
            """.format(**filenames)
        html += """
            </div>
        """.format(**filenames)
        return _Popover(self, "Get this image in your favorite format", content=html,
                        trigger="hover click")

    def _filename_relative_to_out_dir(self, abs_filename: str) -> str:
        ret = os.path.realpath(abs_filename)[len(os.path.realpath(self.misc["out"])) + 1: ]
        if ret == "":
            return "."
        return ret

    _boxplot_cache = {}
    _boxplot_async_cache = {}

    def _singles_property_boxplot(self, obj: SinglesProperty, fig_width: int = None, big: bool = False):
        if fig_width is None:
            fig_width = self.misc["fig_width_big"] if big else self.misc["fig_width_small"]
        filename = self._get_fig_filename(obj) + "___{}".format(fig_width)
        if filename not in self._boxplot_async_cache:
            d = {
                "img": filename + BaseStatObject.img_filename_ending
            }
            if self.misc["gen_tex"]:
                d["tex"] = filename + ".tex"
                d["tex_standalone"] = filename + "____standalone.tex"
            if self.misc["gen_pdf"]:
                d["pdf"] = filename + ".pdf"
            self._boxplot_cache[filename] = d
            self._boxplot_async_cache[filename] = {
                "filename": filename,
                "obj": obj,
                "fig_width": fig_width,
                "img": True,
                "tex": self.misc["gen_tex"],
                "pdf": self.misc["gen_pdf"],
                "tex_sa": self.misc["gen_tex"],
                "zoom_in": self.zoom_in
            }
        return self._boxplot_cache[filename]

    def _process_boxplot_cache_entry(self, entry: t.Dict[str, str]):
        height = self.misc["boxplot_height"] * len(entry["obj"].singles) + 2
        entry["obj"].boxplot(fig_width=entry["fig_width"],
                             fig_height=height, zoom_in=entry["zoom_in"])
        entry["obj"].store_figure(entry["filename"], fig_width=entry["fig_width"], img=entry["img"], tex=entry["tex"],
                              pdf=entry["pdf"], tex_standalone=entry["tex_sa"], fig_height=height, zoom_in=entry["zoom_in"])
        logging.debug("Plotted {}, fig_width={}cm, img={}, tex={}, pdf={}"
                     .format(entry["obj"], entry["fig_width"],
                     entry["img"], entry["tex"], entry["pdf"]))


    _hist_cache = {} # type: t.Dict[str, t.Dict[str, str]]
    _hist_async_img_cache = {}
    _hist_async_misc_cache = {}

    def _histogram(self, obj: BaseStatObject, fig_width: int = None, zoom_in: bool = False,
                   big: bool = False) -> t.Dict[str, str]:
        if fig_width is None:
            fig_width = self.misc["fig_width_big"] if big else self.misc["fig_width_small"]
        filename = self._get_fig_filename(obj) + "___{}___{}".format(fig_width, zoom_in)
        if filename not in self._hist_cache:
            d = {
                "img": filename + BaseStatObject.img_filename_ending
            }
            if self.misc["gen_tex"]:
                d["tex"] = filename + ".tex"
                d["tex_standalone"] = filename + "____standalone.tex"
            if self.misc["gen_pdf"]:
                d["pdf"] = filename + ".pdf"
            self._hist_cache[filename] = d
            self._hist_async_img_cache[filename] = {
                "filename": filename,
                "obj": obj,
                "fig_width": fig_width,
                "zoom_in": zoom_in,
                "img": True,
                "tex": False,
                "pdf": False,
                "tex_sa": False
            }
            if self.misc["gen_pdf"] or self.misc["gen_tex"]:
                self._hist_async_misc_cache[filename] = {
                    "filename": filename,
                    "obj": obj,
                    "fig_width": fig_width,
                    "zoom_in": zoom_in,
                    "img": False,
                    "tex": self.misc["gen_tex"],
                    "pdf": self.misc["gen_pdf"],
                    "tex_sa": self.misc["gen_tex"]
                }
        return self._hist_cache[filename]


    def _process_hist_cache_entry(self, entry: t.Dict[str, str]):
        entry["obj"].histogram(zoom_in=entry["zoom_in"], fig_width=entry["fig_width"])
        entry["obj"].store_figure(entry["filename"], fig_width=entry["fig_width"], img=entry["img"], tex=entry["tex"],
                              pdf=entry["pdf"], tex_standalone=entry["tex_sa"])
        logging.debug("Plotted {}, zoom_in={}, fig_width={}cm, img={}, tex={}, pdf={}"
                     .format(entry["obj"], entry["zoom_in"], entry["fig_width"],
                     entry["img"], entry["tex"], entry["pdf"]))

    def _popover_for_tester(self, tester: Tester):
        return _Popover(self, tester.name.capitalize(), """
                    Probability that the null hypothesis is not incorrect. It's the probability that the measured
                    values (for a given property) come out of the same population for both benchmarked programs.
                    Or short: That the programs have the same characteristics for a given property. <br/>
                    <b>Important note</b>: Statistical tests can only given an probability of the null hypothesis being incorrect.
                    But this okay, if your aim is to see whether a specific program is better (different) than another
                    program in some respect. <br/>
               """)

    def _short_summary_modal(self, obj: BaseStatObject) -> str:
        """

        :param obj:
        :return: id
        """
        if not hasattr(self, "_modal_cache"):
            self._modal_cache = [] # type: t.List[str]
        modal_id = self._html_id_for_object("short_summary_modal", obj)
        if modal_id in self._modal_cache:
            return modal_id
        modal_title = self._obj_description(obj)
        modal_body = self._short_summary(obj, with_title=False)
        html_id = self._html_id_for_object("misc", obj)
        html = """
            <div class="modal fade" id="{modal_id}" tabindex="-10" role="dialog">
              <div class="modal-dialog" role="document">
                <div class="modal-content">
                  <div class="modal-header">
                    <button type="button" class="close" data-dismiss="modal"><span>&times;</span></button>
                    <h4 class="modal-title" id="{modal_id}_label"><a href="#{html_id}">{modal_title}</a></h4>
                  </div>
                  <div class="modal-body">
                    {modal_body}
                  </div>
                  <div class="modal-footer">
                    <button type="button" class="btn btn-default" data-dismiss="modal">Close</button>
                    <button type="button" class="btn btn-primary" data-dismiss="modal"
                        onclick="window.location='#{html_id}'">More information</button>
                  </div>
                </div>
              </div>
            </div>
        """.format(**locals())
        self._app_html += html
        return modal_id

    def _obj_description(self, obj: BaseStatObject) -> str:
        if isinstance(obj, Single):
            return obj.description()
        if isinstance(obj, TestedPair):
            return "{} vs. {}".format(self._obj_description(obj.first), self._obj_description(obj.second))
        if isinstance(obj, SingleProperty) or isinstance(obj, TestedPairProperty):
            obj_base = ""
            if isinstance(obj, SingleProperty):
                obj_base = obj.rundata.description()
            else:
                obj_base = self._obj_description(obj.parent)
            return obj_base + " (regarding {})".format(obj.property)

    def _html_id_for_object(self, scope: str, obj: BaseStatObject) -> str:
        return "{}___{}".format(scope, self._get_obj_id(obj))

    def _get_obj_id(self, obj: BaseStatObject) -> str:
        if isinstance(obj, Single):
            return str(self.stats.singles.index(obj))
        if isinstance(obj, TestedPair):
            return self._get_obj_id(obj.first) + "_" + self._get_obj_id(obj.second)
        if isinstance(obj, SingleProperty) or isinstance(obj, TestedPairProperty):
            return self._get_obj_id(obj.parent) + "__" + self.html_escape_property(obj.property)
        if isinstance(obj, SinglesProperty):
            return "SinglesProperty______" + self.html_escape_property(obj.property)
        assert False # you shouldn't reach this point

    @classmethod
    def html_escape_property(cls, property: str) -> str:
        return re.sub(r"([^a-zA-Z0-9]+)", "000000", property)

    def _format_errors_and_warnings(self, obj: BaseStatObject, show_parent: bool = True) -> str:

        def format_msg(msg: StatMessage):
            message = msg.generate_msg_text(show_parent)
            msg_class = "div_danger" if msg.type == StatMessageType.ERROR else "div_warning"

            html = """
                <div class="panel-body {msg_class}">
                    {message}
                </div>
            """.format(**locals())
            if msg.hint != "" and msg.hint is not None:
                html = """
                    <div tabindex="0" class="panel-body {msg_class}" data-content="{msg.hint}"
                        data-trigger="hover" data-toggle="popover" data-placement="auto top" data-title="Hint">
                        {message}
                    </div>
                """.format(**locals())
            return html

        def collapsible(title: str, msgs: t.List[StatMessage]):
            collapse_id = self._random_html_id()
            heading_id = self._random_html_id()
            inner = "\n".join(map(format_msg, msgs))
            return """
                <div class="panel-group" role="tablist">
                <div class="panel panel-default">
                    <div class="panel-heading" role="tab" id="{heading_id}">
                      <h4 class="panel-title">
                        <a role="button" data-toggle="collapse" href="#{collapse_id}" style="display: block">
                            {title}
                        </a>
                      </h4>
                    </div>
                    <div id="{collapse_id}" class="panel-collapse collapse" role="tabpanel">
                        {inner}
                    </div>
                  </div>
                </div>
            """.format(**locals())
        html = ""
        if not self._hide_stat_warnings:
            if obj.has_errors():
                html += collapsible('Severe warnings <span class="badge">{}</span>'.format(len(obj.errors())), obj.errors())
            if obj.has_warnings():
                html += collapsible('Warnings <span class="badge">{}</span>'.format(len(obj.warnings())), obj.warnings())
        return html

    _time = time.time()

    def _get_fig_filename(self, obj: BaseStatObject) -> str:
        """ Without any extension. """
        return os.path.realpath(os.path.join(os.path.abspath(self.misc["out"]),
                                             self._html_id_for_object("fig", obj)))

    _id_counter = 1000

    def _random_html_id(self) -> str:
        self._id_counter += 1
        return "id" + str(self._id_counter)

    def get_random_filename(self) -> str:
        return os.path.realpath(os.path.join(os.path.abspath(self.misc["out"]), self._random_html_id()))

class _Popover:

    divs = {} # t.Dict[str, str]
    """ Maps the contents of the created divs to their ids """

    def __init__(self, parent: HTMLReporter2, title: str, content: str, trigger: str = "hover"):
        self.parent = parent
        self.title = title
        self.content = content or ""
        self.trigger = trigger

    def __str__(self) -> str:
        content = """<div class='hyphenate'>""" + self.content + """</div>"""
        if content not in self.divs:
            id = self.parent._random_html_id()
            self.parent._app_html += """
            <div style="display: none" id="{id}">
            {content}
            </div>
            """.format(id=id, content=content)
            self.divs[content] = id
        id = self.divs[content]
        focus = 'tabindex="0" role="button"' if "focus" in self.trigger or "click" in self.trigger else ""
        return '{focus} data-trigger="{trigger}" data-toggle="popover" data-html="true"' \
                'data-placement="auto" data-title="{title}" data-container="body" ' \
                'data-content-id="{id}"'\
                .format(content=content, trigger=self.trigger, title=self.title, focus=focus, id=id)


def _color_class(obj: BaseStatObject) -> str:
    if obj.has_errors():
        return "error"
    if obj.has_warnings():
        return "warning"
    if isinstance(obj, TestedPairProperty):
        if obj.is_equal() is not None:
            return "success" if obj.is_equal() is False and obj.mean_diff_per_mean() < 1 else "active"
    return ""


def _color_explanation(obj: BaseStatObject) -> str:
    color_class = "div_" + _color_class(obj)
    msg = ""
    if obj.has_errors():
        msg = "This color means that there are severe warnings related to the corresponding data set " \
              "({} severe warning(s) and {} warning(s)).".format(len(obj.errors()), len(obj.warnings()))
    elif obj.has_warnings():
        msg = "This color means that there are warnings related to the corresponding data set " \
              "(with {} warning(s)).".format(len(obj.warnings()))
    elif isinstance(obj, TestedPairProperty) and obj.is_equal() is not None:
        msg = "This color means that everything is probably okay with the corresponding data" \
              " and that the tester could make a decision."
    else:
        msg = "Everything seems to be okay."
    if msg != "":
        return """
            <p class='{color_class}'>
                {msg}
            </p>
        """.format(**locals())


class _Cell:
    """
    Cell of a html table
    """

    def __init__(self, parent: HTMLReporter2, content: str = "", cell_class: str = "", popover: _Popover = None,
                 modal_id: str = None, color_class_obj: BaseStatObject = None,
                 is_header_cell: bool = False, cell_scope: str = None,
                 show_click_on_info: bool = None, link: str = None):
        """
        :param content: displayed text of the cell
        :param cell_class: CSS class of the table cellr
        :param modal_id: id of the modal linked to this cell
        :param color_class_obj: object used to get the color class. Adds also an explanation to the popover
        :param is_header_cell: is the cell a header cell?
        """
        self.content = content
        self.cell_class = cell_class
        self.popover = popover
        self.modal_id = modal_id
        self.link = link
        self.parent = parent
        assert link is None or modal_id is None
        if color_class_obj is not None:
            warnings_text = "" if self.parent._hide_stat_warnings \
                            else _color_explanation(color_class_obj)
            if self.popover is None:
                self.popover = _Popover(parent, "Explanation", warnings_text)
            else:
                self.popover.content += warnings_text
            if not self.parent._hide_stat_warnings:
                self.cell_class += " " + _color_class(color_class_obj)
        if (modal_id is not None and show_click_on_info != False) or (show_click_on_info is True and not link):
            msg = "<p>Click on the cell to get more information.</p>"
            if self.popover is None:
                self.popover = _Popover(parent, "Explanation", msg)
            else:
                self.popover.content += msg
        self.is_header_cell = is_header_cell
        self.cell_scope = cell_scope

    def __str__(self):
        cell_tag = "th" if self.is_header_cell else "td"
        scope = 'scope="{}"'.format(self.cell_scope) if self.cell_scope else ""
        html = """<{} class="{}" {}>""".format(cell_tag, self.cell_class, scope)
        html_end = "</{}>".format(cell_tag)
        if self.popover:
            html += """<div style="width: 100%" {}>""".format(self.popover)
            html_end = "</div>" + html_end
        if self.modal_id:
            html += """<a data-toggle="modal" data-target="#{id}" style="width:100%;">""".format(id=self.modal_id)
            html_end = "</a>" + html_end
        if self.link:
            html += """
                <a href="{link}" onclick="scrollTo('{elem_id}', '{link}')" data-dismiss="modal" style="width:100%;">
                """.format(link=self.link, elem_id=self.parent._random_html_id())
            html_end = "</a>" + html_end
        return html + self.content + html_end


T1 = t.TypeVar('T1', BaseStatObject, str, int, float, bool)
T2 = t.TypeVar('T2', BaseStatObject, str, int, float, bool)


class _Table:
    """
    A html table consisting of Cell objects.
    Idea: Abstract the creation of html tables to a degree that allows automatic generation of latex and csv.
    """

    def __init__(self, parent: HTMLReporter2, header_row: t.List['_Cell'], header_col: t.List['_Cell'],
                 anchor_cell: '_Cell' = None, content_cells: t.List[t.List['_Cell']] = None):
        """
        The resulting table has len(header_row) + rows and len(header_col) + 1 columns.

        :param header_row: list of cells of the bold top header row
        :param header_col: list of cells of the bold left header collumn
        :param anchor_cell: the cell in the top left corner of the table
        :param content_cells: a list of content rows
        :return: resulting html
        """
        self.parent = parent
        self.header_row = header_row
        self.header_col = header_col
        for cell in itertools.chain(self.header_row, self.header_col):
            cell.is_header_cell = True
        for cell in self.header_col:
            cell.cell_scope = "row"
        assert len(header_row) > 0
        self.orig_anchor_cell = _Cell(self.parent, "") if anchor_cell is None else _Cell(self.parent, anchor_cell.content)
        self.anchor_cell = anchor_cell or _Cell(self.parent, "&#9047; ")
        self.anchor_cell.content += "  	&#9047;"
        self.anchor_cell.cell_class += " anchor_cell "
        self.height = len(header_col)
        """ Number of content (non header) rows """
        self.width = len(header_row)
        """ Number of content (non header) columns """
        if content_cells:
            assert len(content_cells) == self.height and len(content_cells[0]) == self.width \
                                        and all(len(content_cells[0]) == len(row) for row in content_cells)
            self.content_cells = content_cells
        else:
            self.content_cells = [[_Cell(self.parent) for i in range(self.width)] for j in range(self.height)]

    def __str__(self) -> str:
        html = """
        <table class="table">
            <thead>
        """
        html += " ".join(str(cell) for cell in [self.format_anchor_cell()] + self.header_row)
        html += """
            </thead>
            <tbody>
        """
        for (hcell, row) in zip(self.header_col, self.content_cells):
            html += "\t\t\t<tr>{}</tr>\n".format(" ".join(str(cell) for cell in [hcell] + row))
        html += """
            </tbody>
        </table>
        """
        return html

    def html(self):
        return str(self)

    def format_anchor_cell(self) -> '_Cell':
        formats = [{
            "ending": ".tex",
            "mime": "application/x-latex",
            "descr": "Latex table (requires package <code>booktabs</code>)",
            "code": self.latex()
         }, {
            "ending": ".tex",
            "mime": "application/x-latex",
            "descr": "Latex table with surrounding article environment",
            "code": self.latex(True)
         }, {
            "ending": ".csv",
            "mime": "text/csv",
            "descr": "CSV table",
            "code": self.csv()
        }]
        html = """
            <div class='list-group'>
        """
        for d in formats:
            id = self.parent._random_html_id()

            self.parent._app_html += """
                <pre id="{}" style="display: none;">
                    {}
                </pre>
            """.format(id, d["code"])
            html += """
                  <div onclick='download(this)' code_id='{id}' mime='{mime}'
                        filename='{filename}'class='list-group-item'
                        style='cursor: pointer'>
                    {descr}
                  </div>
            """.format(descr=d["descr"], id=id, filename="table" + d["ending"], mime=d["mime"])
        if self.parent.misc["gen_xls"]:
            html += """
                <a href='{filename}' class='list-group-item'>
                    Excel (.xls) file
                </a>
            """.format(filename=self.xls())
        html += """
            </div>
        """
        self.anchor_cell.popover = _Popover(self.parent, "Get this table in your favorite format", content=html,
                                            trigger="hover click")
        return self.anchor_cell

    def latex(self, with_env: bool = False) -> str:
        tex = ""
        tex_end = ""
        if with_env:
            tex = """
\\documentclass[10pt,a4paper]{article}
\\usepackage{booktabs}
\\begin{document}
            """
            tex_end = """
\\end{document}
"""
        tex += """
    \\begin{{tabular}}{{l{cs}}}\\toprule
        """.format(cs="".join("r" * self.width))
        tex_end = """
        \\bottomrule
    \\end{tabular}
        """ + tex_end
        tex += " & ".join(cell.content for cell in [self.orig_anchor_cell] + self.header_row) + "\\\\ \n \\midrule "
        for (hcell, row) in zip(self.header_col, self.content_cells):
            tex += " & ".join(cell.content.replace("%", "\\%").replace("_", "\\_") for cell in [hcell] + row) + "\\\\ \n"
        return tex + tex_end

    def csv(self) -> str:
        rows = []
        rows.append(",".join(repr(cell.content) for cell in [self.orig_anchor_cell] + self.header_row))

        def convert_content(text: str) -> str:
            try:
                if text.endswith("%"):
                    return str(float(text[:-1]) / 100)
                float(text)
                return text
            except:
                return repr(text)

        for (hcell, row) in zip(self.header_col, self.content_cells):
            rows.append(",".join(convert_content(cell.content) for cell in [hcell] + row))
        return "\n".join(rows)

    def xls(self) -> str:
        import tablib
        data = tablib.Dataset()
        data.headers = [cell.content for cell in [self.orig_anchor_cell] + self.header_row]
        for (hcell, row) in zip(self.header_col, self.content_cells):
            data.append([cell.content for cell in [hcell] + row])
        filename = self.parent.get_random_filename() + ".xls"
        with open(filename, "wb") as f:
            f.write(data.xls)
            chown(f)
        return filename


    def __getitem__(self, cell_pos: t.Tuple[int, int]) -> '_Cell':
        return self.content_cells[cell_pos[0]][cell_pos[1]]

    def __setitem__(self, cell_pos: t.Tuple[int, int], new_val: '_Cell'):
        self.content_cells[cell_pos[0]][cell_pos[1]] = new_val

    def append(self, header: '_Cell', content_row: t.List['_Cell']):
        assert len(content_row) == self.width
        self.content_cells.append(content_row)
        self.header_col.append(header)

    @classmethod
    def from_content_func(cls, parent: HTMLReporter2, rows: t.List[T1], cols: t.List[T2], anchor_cell: '_Cell',
                          content_func: t.Callable[[T1, T2], Any],
                          content_modal_func: t.Callable[[T1, T2, int, int], str] = None,
                          header_modal_func: t.Callable[[t.Union[T1, T2], int, bool], str] = None,
                          content_popover_func: t.Callable[[T1, T2, int, int], t.Optional[_Popover]] = None,
                          header_popover_func: t.Callable[[t.Union[T1, T2], int, bool], t.Optional[_Popover]] = None,
                          content_link_func: t.Callable[[T1, T2, int, int], t.Optional[str]] = None,
                          header_link_func: t.Callable[[t.Union[T1, T2], int, bool], t.Optional[str]] = None,
                          content_color_obj_func: t.Callable[[T1, T2, int, int], t.Optional[BaseStatObject]] = None,
                          header_color_obj_func: t.Callable[[t.Union[T1, T2], int, bool],
                                                            t.Optional[BaseStatObject]] = None,
                          header_content_func: t.Callable[[t.Union[T1, T2], int, bool], str] = None):
        """
        Idea: Table that populates itself with a passed content function.
        """
        def convert_hc(elem: t.Union[T1, T2], index: int, header_row: bool) -> _Cell:
            def call(func: t.Optional[t.Callable[[t.Union[T1, T2], int, bool], t.T]]) -> t.T:
                if func:
                    return func(elem, index, header_row)
                return None
            content = ""
            color_obj = None
            if header_content_func:
                content = str(header_content_func(elem, index, header_row))
            elif isinstance(elem, str) or isinstance(elem, int) or isinstance(elem, float):
                content = str(elem)
            elif isinstance(elem, BaseStatObject):
                content = parent._obj_description(elem)
            else:
                assert False
            if isinstance(elem, BaseStatObject):
                color_obj = elem
            if header_color_obj_func:
                color_obj = header_color_obj_func(elem, index, header_row)
            modal_id = call(header_modal_func)
            popover = call(header_popover_func)
            link = None
            if header_link_func and header_link_func(elem, index, header_row):
                assert not modal_id # modal and link can't be used together in the same cell
                link = header_link_func(elem, index, header_row)
            return _Cell(parent, content, popover=popover, modal_id=modal_id, color_class_obj=color_obj, is_header_cell=True,
                         cell_scope="row" if header_row else None, link=link)
        header_row = []
        for (i, elem) in enumerate(cols):
            header_row.append(convert_hc(elem, i, header_row=True))
        header_col = []
        for (i, elem) in enumerate(rows):
            header_col.append(convert_hc(elem, i, header_row=False))

        def convert_cc(row_header: T1, col_header: T2, row: int, col: int) -> _Cell:
            def call(func: t.Optional[t.Callable[[T1, T2, int, int], t.T]]) -> t.T:
                if func:
                    return func(row_header, col_header, row, col)
                return None

            content = str(content_func(row_header, col_header, row, col))
            color_obj = call(content_color_obj_func)
            modal_id = call(content_modal_func)
            popover = call(content_popover_func)
            link = call(content_link_func)
            assert None in [link, modal_id]
            return _Cell(parent, content, popover=popover, modal_id=modal_id, color_class_obj=color_obj, link=link)
        content_cells = []
        for (row, row_header) in enumerate(rows):
            a = []
            for (col, col_header) in enumerate(cols):
                a.append(convert_cc(row_header, col_header, row, col))
            content_cells.append(a)
        return _Table(parent, header_row, header_col, anchor_cell, content_cells)


def html_escape_property(property: str) -> str:
    """
    Escape the name of a measured property.

    :param property: name of a measured property
    :return: escaped property name
    """
    return re.sub(r"([^a-zA-Z0-9]+)", "000000", property)


valid_csv_reporter_modifiers = ["mean", "stddev", "property", "min", "max", "stddev per mean"]  # type: t.List[str]


FORMAT_OPTIONS = {
    "%": "format as percentage",
    "p": "wrap insignificant digits in parentheses (+- 2 std dev)",
    "s": "use scientific notation, configured in report/number",
    "o": "wrap digits in the order of magnitude of 2 std devs in parentheses"
}


def _parse_csv_reporter_specs(specs: t.List[str]) -> t.List[t.Tuple[str, str, t.Set[str]]]:
    return list(itertools.chain.from_iterable(_parse_csv_reporter_spec(spec) for spec in specs))


def _parse_csv_reporter_spec(spec: str) -> t.List[t.Tuple[str, str, t.Set[str]]]:
    return list(map(_parse_csv_reporter_spec_single, spec.split(",")))


def _parse_csv_reporter_spec_single(spec: str) -> t.Tuple[str, str, t.Set[str]]:
    def error():
        raise SyntaxError("Column spec {!r} isn't valid".format(spec))

    parts = spec.strip().split("[")
    if len(parts) == 1 and parts[0] != "":
        return parts[0], ""
    if len(parts[1]) < 2 or "]" not in parts[1]:
        error()
    prop, opt = (parts[1][:-1] + "|").split("|")[0:2]
    if parts[1][:-1].split("|")[0] not in valid_csv_reporter_modifiers or \
            len(parts[0]) < 1 or any(x not in FORMAT_OPTIONS for x in opt):
        error()
    return parts[0], prop, [x for x in opt if x != " "]


def _is_valid_csv_reporter_spec_list(specs: t.List[str]) -> bool:
    try:
        _parse_csv_reporter_specs(specs)
    except SyntaxError as err:
        return False
    return True

@register(ReporterRegistry, "csv", Dict({
    "out": FileNameOrStdOut() // Default("-") // Description("Output file name or standard out (-)"),
    "columns": ListOrTuple(Str()) // (lambda x: _is_valid_csv_reporter_spec_list(x))
               // Description("List of valid column specs, format is a comma separated list of 'PROPERTY\\[mod\\]' or 'ATTRIBUTE' "
                              "mod is one of: {}, optionally a formatting option can be given via"
                              "PROPERTY\\[mod|OPT1OPT2…\\], where the OPTs are one of the following: {}. "
                              "PROPERTY can be either the description or the short version of the property. "
                              "Configure the number formatting further via the number settings in the settings file"
                              .format(join_strs(valid_csv_reporter_modifiers),
                                      join_strs("{} ({})".format(k, v) for k, v in FORMAT_OPTIONS.items())))
    // Default(["description"])
}))
class CSVReporter(AbstractReporter):
    """
    Simple reporter that outputs just a configurable csv table with rows for each run block
    """

    def report(self) -> t.Optional[str]:
        """
        Create an report and output it as configured.

        :return: the report string if ``to_string == True``
        """
        if not self.misc["out"] == "-" and not os.path.exists(os.path.dirname(self.misc["out"])):
            logging.error("Folder for report ({}) doesn't exist".format(os.path.dirname(self.misc["out"])))
            exit(1)
        with click.open_file(self.misc["out"], mode='w') as f:
            import tablib
            data = tablib.Dataset(itertools.chain.from_iterable(x.split(",") for x in self.misc["columns"]))
            for row in self._table():
                data.append(row)
            f.write(data.csv)
            chown(f)

    def _table(self) -> t.List[t.List[t.Union[str, int, float]]]:
        table = []
        specs = _parse_csv_reporter_specs(self.misc["columns"])
        for single in self.stats.singles:
            table.append(self._row(single, specs))
        return table

    def _row(self, single: Single, specs: t.List[t.Tuple[str, str, t.List[str]]]) -> t.List[t.Union[str, int, float]]:
        return [self._column(single, spec) for spec in specs]

    def _column(self, single: Single, spec: t.Tuple[str, str]) -> t.Union[str, int, float]:
        if spec[1] == "":
            return single.attributes[spec[0]]
        long_prop = self.to_long_prop_dict[spec[0]] if spec[0] in self.to_long_prop_dict else spec[0]
        if long_prop is None or long_prop not in single.properties:
            raise SyntaxError("No such property {}".format(long_prop))
        return self._column_property(single.properties[long_prop], spec[1], spec[2])

    def _column_property(self, single: SingleProperty, modifier: str, opts: t.List[str],
                         baseline: SingleProperty = None) -> t.Union[str, int, float]:
        mod = {
            "mean": lambda single: single.mean(),
            "stddev": lambda single: single.std_dev(),
            "property": lambda single: single.property,
            "description": lambda single: single.description(),
            "min": lambda single: single.min(),
            "max": lambda single: single.max(),
            "stddev per mean": lambda single: single.std_dev_per_mean()
        }
        num = mod[modifier](single)
        if baseline:
            num = num / baseline.mean()
        return FNumber(num,
                       abs_deviation=single.std_dev(),
                       is_percent=("%" in opts),
                       scientific_notation=("s" in opts),
                       parentheses=("o" in opts or "p" in opts),
                       parentheses_mode=ParenthesesMode.DIGIT_CHANGE if "p" in opts else \
                                        (ParenthesesMode.ORDER_OF_MAGNITUDE if "o" in opts else None)).format()


@register(ReporterRegistry, "codespeed", Dict({
    "project": Str() // Default("") // Description("Project name reported to codespeed."),
    "executable": Str() // Default("") // Description("Executable name reported to codespeed. Defaults to the project name."),
    "environment": Str() // Default("") // Description("Environment name reported to codespeed. Defaults to current host name."),
    "branch": Str() // Default("") // Description("Branch name reported to codespeed. Defaults to current branch or else 'master'."),
    "commit_id": Str() // Default("") // Description("Commit ID reported to codespeed. Defaults to current commit."),
}))
class CodespeedReporter(AbstractReporter):
    """
    Reporter that outputs JSON as expected by `codespeed <https://github.com/tobami/codespeed>`_.
    Branch name and commit ID are taken from the current directory.
    Use it like this:

    .. code:: sh

        temci report --reporter codespeed ... | curl --data-urlencode json@- http://localhost:8000/result/add/json/

    """

    def report(self):
        """
        Create a report and output it as configured.
        """
        import json, platform
        from temci.utils.vcs import VCSDriver
        vcs_driver = VCSDriver.get_suited_vcs()
        branch = vcs_driver.get_branch()
        self.meta = {
            "project": self.misc["project"],
            "executable": self.misc["executable"] or self.misc["project"],
            "environment": self.misc["environment"] or platform.node(),
            "branch": self.misc["branch"] or branch or "master",
            "commitid": self.misc["commit_id"] or (branch and vcs_driver.get_info_for_revision(branch)["commit_id"]),
        }
        data = [self._report_prop(run, prop)
                for run in self.stats_helper.runs
                for prop in sorted(run.properties)]
        json.dump(data, sys.stdout)

    def _report_prop(self, run: RunData, prop: str) -> dict:
        return {
            **self.meta,
            "benchmark": "{}: {}".format(run.description(), prop),
            "result_value": np.mean(run[prop]),
            "std_dev": np.std(run[prop]),
            "min": min(run[prop]),
            "max": max(run[prop]),
        }


@register(ReporterRegistry, "codespeed2", Dict({}))
class Codespeed2Reporter(AbstractReporter):
    """
    Reporter that outputs JSON as specified by
    the `codespeed runner spec <https://git.scc.kit.edu/IPDSnelting/codespeed-runner>`_.
    """

    def report(self):
        """
        Create a report and output it as configured.
        """
        import json
        res = {}
        for run in self.stats_helper.errorneous_runs:
            bench_res = {}
            for prop in run.properties:
                bench_res[prop] = {
                    "error": run.recorded_error.message
                }
            res[run.description()] = bench_res
        for run in self.stats_helper.runs:
            bench_res = {}
            for prop in run.properties:
                bench_res[prop] = {
                    "results": run[prop],
                    "unit": ("s" if "time" in prop or "clock" in prop else prop),
                    "resultInterpretation": "LESS_IS_BETTER"
                }
            res[run.description()] = bench_res
        json.dump(res, sys.stdout)


@register(ReporterRegistry, "velcom", Dict({}))
class VelcomReporter(AbstractReporter):
    """
    Reporter that outputs JSON as specified by
    the `velcom runner spec <https://github.com/IPDSnelting/velcom/wiki/Benchmark-Repo-Specification>`_.
    """

    def report(self):
        """
        Create a report and output it as configured.
        """
        import json
        res = {}
        if len(self.stats_helper.errorneous_runs) > 0 and len(self.stats_helper.runs) == 0:
            return json.dump({
                "error": "\n".join("[{}] {}".format(run.description(), run.recorded_error.message)
                                   for run in self.stats_helper.errorneous_runs)
            }, sys.stdout)
        for run in self.stats_helper.errorneous_runs:
            bench_res = {}
            for prop in run.properties:
                bench_res[prop] = {
                    "error": run.recorded_error.message
                }
            res[run.description()] = bench_res
        for run in self.stats_helper.runs:
            bench_res = {}
            for prop in run.properties:
                bench_res[prop] = {
                    "values": run[prop],
                    "unit": ("s" if "time" in prop or "clock" in prop else prop),
                    "interpretation": "LESS_IS_BETTER"
                }
            res[run.description()] = bench_res
        json.dump(res, sys.stdout)
