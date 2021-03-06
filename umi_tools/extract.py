'''
extract.py - Extract UMI from fastq
====================================================

:Author: Ian Sudbery, Tom Smith
:Release: $Id$
:Date: |today|
:Tags: Python UMI

Purpose
-------

Extract UMI barcode from a read and add it to the read name, leaving
any sample barcode in place. Can deal with paired end reads and UMIs
split across the paired ends. Can also optionally extract cell
barcodes and append these to the read name also. See the section below
for an explanation for how to encode the barcode pattern(s) to
specficy the position of the UMI +/- cell barcode.


Filtering and correcting cell barcodes
--------------------------------------

umi_tools extract can optionally filter cell barcodes
(--filter-cell-barcode) against a user-supplied whitelist
(--whitelist). If a whitelist is not available for your data, e.g
if you have performed droplet-based scRNA-Seq, you can use the
whitelist tool.

Cell barcodes which do not match the whitelist (user-generated or
automatically generated) can also be optionally corrected using the
--error-correct-cell option.

The whitelist should be in the following format (tab-separated):

    AAAAAA	AGAAAA
    AAAATC
    AAACAT
    AAACTA	AAACTN,GAACTA
    AAATAC
    AAATCA	GAATCA
    AAATGT	AAAGGT,CAATGT

Where column 1 is the whitelisted cell barcodes and column 2 is
the list (comma-separated) of other cell barcodes which should be
corrected to the barcode in column 1. If the --error-correct-cell
option is not used, this column will be ignored. Any additional columns
in the whitelist input, such as the counts columns from the output of
umi_tools whitelist, will be ignored.


Experimental options
--------------------
If you have a library preparation method where the UMI may be in
either read, you can use --either-read --extract-method
--bc-pattern=[PATTERN1] --bc-pattern2=[PATTERN2] to search for the UMI
in either. See the section 'regex' in umi_tools extract --help for
further details about using a regex pattern. Where both patterns
match, the default behaviour is to discard both reads. If you want to
select the read with the UMI with highest sequence quality, provide
--either-read-resolve=quality.

Usage:
------

For single ended reads:
        umi_tools extract --extract-method=string
        --bc-pattern=[PATTERN] -L extract.log [OPTIONS]

reads from stdin and outputs to stdout.

For paired end reads:
        umi_tools extract --extract-method=string
        --bc-pattern=[PATTERN] --bc-pattern2=[PATTERN]
        --read2-in=[FASTQIN] --read2-out=[FASTQOUT] -L extract.log [OPTIONS]

reads end one from stdin and end two from FASTQIN and outputs end one to stdin
and end two to FASTQOUT.


Using regex and filtering against a whitelist of cell barcodes:
        umi_tools extract --extract-method=regex --filter-cell-barcode
        --bc-pattern=[REGEX] --whitlist=[WHITELIST_TSV]
        -L extract.log [OPTIONS]

'''
from __future__ import absolute_import
import sys
import regex
import collections
import optparse

# python 3 doesn't require izip
try:
    # Python 2
    from itertools import izip
except ImportError:
    # Python 3
    izip = zip

import umi_tools.Utilities as U
import umi_tools.umi_methods as umi_methods

# add the generic docstring text
__doc__ = __doc__ + U.GENERIC_DOCSTRING_WE


def main(argv=None):
    """script main.

    parses command line options in sys.argv, unless *argv* is given.
    """

    if argv is None:
        argv = sys.argv

    # setup command line parser
    parser = U.OptionParser(version="%prog version: $Id$",
                            usage=globals()["__doc__"])

    group = U.OptionGroup(parser, "extract-specific options")

    # (Experimental option) Retain the UMI in the sequence read"
    group.add_option("--retain-umi", dest="retain_umi", action="store_true",
                     help=optparse.SUPPRESS_HELP)
    group.add_option("--read2-out", dest="read2_out", type="string",
                     help="file to output processed paired read to")
    group.add_option("--read2-stdout", dest="read2_stdout",
                     action="store_true",
                     help="Paired reads, send read2 to stdout, discarding read1")
    group.add_option("--quality-filter-threshold",
                     dest="quality_filter_threshold", type="int",
                     help=("Remove reads where any UMI base quality score "
                           "falls below this threshold"))
    group.add_option("--quality-filter-mask",
                     dest="quality_filter_mask", type="int",
                     help=("If a UMI base has a quality below this threshold, "
                           "replace the base with 'N'"))
    group.add_option("--quality-encoding",
                     dest="quality_encoding", type="choice",
                     choices=["phred33", "phred64", "solexa"],
                     help=("Quality score encoding. Choose from 'phred33'"
                           "[33-77] 'phred64' [64-106] or 'solexa' [59-106]"))
    group.add_option("--filter-cell-barcode",
                     dest="filter_cell_barcode",
                     action="store_true",
                     help="Filter the cell barcodes")
    group.add_option("--error-correct-cell",
                     dest="error_correct_cell",
                     action="store_true",
                     help=("Correct errors in the cell barcode"))
    group.add_option("--whitelist",
                     dest="whitelist", type="string",
                     help=("A whitelist of accepted cell barcodes"))
    group.add_option("--blacklist",
                     dest="blacklist", type="string",
                     help=("A blacklist of accepted cell barcodes"))
    group.add_option("--subset-reads", "--reads-subset",
                     dest="reads_subset", type="int",
                     help=("Only extract from the first N reads. If N is "
                           "greater than the number of reads, all reads will "
                           "be used"))
    group.add_option("--reconcile-pairs",
                     dest="reconcile", action="store_true",
                     help=("Allow the presences of reads in read2 input that "
                           "are not present in read1 input. This allows cell "
                           "barcode filtering of read1s without "
                           "considering read2s"))

    parser.add_option_group(group)

    group = U.OptionGroup(parser, "[EXPERIMENTAl] barcode extraction options")

    group.add_option("--either-read", dest="either_read", action="store_true",
                     help="UMI may be on either read (see "
                     "--either-read-resolve) for options to resolve cases where"
                     "UMI is on both reads")
    group.add_option("--either-read-resolve",
                     dest="either_read_resolve", type="choice",
                     choices=["discard", "quality"],
                     help=("How to resolve instances where both reads "
                           "contain a UMI but using --either-read."
                           "Choose from 'discard' or 'quality'"
                           "(use highest quality). default=dicard"))

    parser.add_option_group(group)

    parser.set_defaults(extract_method="string",
                        filter_cell_barcodes=False,
                        whitelist=None,
                        blacklist=None,
                        error_correct_cell=False,
                        pattern=None,
                        pattern2=None,
                        read2_in=None,
                        read2_out=False,
                        read2_stdout=False,
                        quality_filter_threshold=None,
                        quality_encoding=None,
                        reconcile=False,
                        either_read=False,
                        either_read_resolve="discard")

    # add common options (-h/--help, ...) and parse command line

    (options, args) = U.Start(parser, argv=argv,
                              add_extract_options=True,
                              add_group_dedup_options=False,
                              add_umi_grouping_options=False,
                              add_sam_options=False)

    if options.retain_umi and not options.extract_method == "regex":
        raise ValueError("option --retain-umi only works with --extract-method=regex")

    if options.quality_filter_threshold or options.quality_filter_mask:
        if not options.quality_encoding:
            U.error("must provide a quality encoding (--quality-"
                    "encoding) to filter UMIs by quality (--quality"
                    "-filter-threshold) or mask low quality bases "
                    "with (--quality-filter-mask)")

    extract_cell, extract_umi = U.validateExtractOptions(options)

    if options.either_read:
        if extract_cell:
            U.error("Option to extract from either read (--either-read) "
                    "is not currently compatible with cell barcode extraction")
        if not options.extract_method == "regex":
            U.error("Option to extract from either read (--either-read)"
                    "requires --extract-method=regex")
        if not options.pattern or not options.pattern2:
            U.error("Option to extract from either read (--either-read)"
                    "requires --bc-pattern=[PATTERN1] and"
                    "--bc-pattern2=[PATTERN2]")

    if options.filter_cell_barcodes:

        if not options.whitelist:
                U.error("must provide a whitelist (--whitelist) if using "
                        "--filter-cell-barcode option")

        if not extract_cell:
            if options.extract_method == "string":
                U.error("barcode pattern(s) do not include any cell bases "
                        "(marked with 'Cs') %s, %s" % (
                            options.pattern, options.pattern2))
            elif options.extract_method == "regex":
                U.error("barcode regex(es) do not include any cell groups "
                        "(starting with 'cell_') %s, %s" (
                            options.pattern, options.pattern2))

    read1s = umi_methods.fastqIterate(options.stdin)

    # set up read extractor
    ReadExtractor = umi_methods.ExtractFilterAndUpdate(
        options.extract_method,
        options.pattern,
        options.pattern2,
        options.prime3,
        extract_cell,
        options.quality_encoding,
        options.quality_filter_threshold,
        options.quality_filter_mask,
        options.filter_cell_barcode,
        options.retain_umi,
        options.either_read,
        options.either_read_resolve)

    if options.filter_cell_barcode:
        cell_whitelist, false_to_true_map = umi_methods.getUserDefinedBarcodes(
            options.whitelist, options.error_correct_cell)

        ReadExtractor.cell_whitelist = cell_whitelist
        ReadExtractor.false_to_true_map = false_to_true_map

    if options.blacklist:
        blacklist = set()
        with U.openFile(options.blacklist, "r") as inf:
            for line in inf:
                blacklist.add(line.strip().split("\t")[0])
        ReadExtractor.cell_blacklist = blacklist

    # variables for progress monitor
    progCount = 0
    displayMax = 100000
    U.info("Starting barcode extraction")

    if options.read2_in is None:
        for read in read1s:

            # incrementing count for monitoring progress
            progCount += 1

            # Update display in every 100kth iteration
            if progCount % displayMax == 0:
                U.info("Parsed {} reads".format(progCount))

            new_read = ReadExtractor(read)

            if options.reads_subset:
                if (ReadExtractor.read_counts['Input Reads'] >
                    options.reads_subset):
                    break

            if not new_read:
                continue

            options.stdout.write(str(new_read) + "\n")

    else:
        read2s = umi_methods.fastqIterate(U.openFile(options.read2_in))

        if options.read2_out:
            read2_out = U.openFile(options.read2_out, "w")

        if options.reconcile:
            strict = False
        else:
            strict = True

        for read1, read2 in umi_methods.joinedFastqIterate(
                read1s, read2s, strict):

            # incrementing count for monitoring progress
            progCount += 1

            # Update display in every 100kth iteration
            if progCount % displayMax == 0:
                U.info("Parsed {} reads".format(progCount))
                sys.stdout.flush()

            reads = ReadExtractor(read1, read2)

            if options.reads_subset:
                if (ReadExtractor.read_counts['Input Reads'] >
                    options.reads_subset):
                    break

            if not reads:
                continue
            else:
                new_read1, new_read2 = reads

            if options.read2_stdout:
                options.stdout.write(str(new_read2) + "\n")
            else:
                options.stdout.write(str(new_read1) + "\n")

                if options.read2_out:
                    read2_out.write(str(new_read2) + "\n")

    if options.read2_out:
        read2_out.close()

    for k, v in ReadExtractor.getReadCounts().most_common():
        U.info("%s: %s" % (k, v))

    U.Stop()

if __name__ == "__main__":
    sys.exit(main(sys.argv))
