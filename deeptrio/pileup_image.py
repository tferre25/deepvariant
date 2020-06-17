# Copyright 2017 Google LLC.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Encodes reference and read data into a PileupImage for DeepTrio."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools



import enum
import numpy as np

from deeptrio import dt_constants
from deepvariant.protos import deepvariant_pb2
from deepvariant.python import pileup_image_native
from third_party.nucleus.protos import reads_pb2
from third_party.nucleus.util import ranges
from third_party.nucleus.util import utils


def default_options(read_requirements=None):
  """Creates a PileupImageOptions populated with good default values."""
  if not read_requirements:
    read_requirements = reads_pb2.ReadRequirements(
        min_base_quality=10,
        min_mapping_quality=10,
        min_base_quality_mode=reads_pb2.ReadRequirements.ENFORCED_BY_CLIENT)

  return deepvariant_pb2.PileupImageOptions(
      reference_band_height=5,
      base_color_offset_a_and_g=40,
      base_color_offset_t_and_c=30,
      base_color_stride=70,
      allele_supporting_read_alpha=1.0,
      allele_unsupporting_read_alpha=0.6,
      other_allele_supporting_read_alpha=0.6,
      reference_matching_read_alpha=0.2,
      reference_mismatching_read_alpha=1.0,
      indel_anchoring_base_char='*',
      reference_alpha=0.4,
      reference_base_quality=60,
      positive_strand_color=70,
      negative_strand_color=240,
      base_quality_cap=40,
      mapping_quality_cap=60,
      height=dt_constants.PILEUP_DEFAULT_HEIGHT,
      height_parent=dt_constants.PILEUP_DEFAULT_HEIGHT_PARENT,
      height_child=dt_constants.PILEUP_DEFAULT_HEIGHT_CHILD,
      width=dt_constants.PILEUP_DEFAULT_WIDTH,
      num_channels=6,
      read_overlap_buffer_bp=5,
      read_requirements=read_requirements,
      multi_allelic_mode=deepvariant_pb2.PileupImageOptions.ADD_HET_ALT_IMAGES,
      # Fixed random seed produced with 'od -vAn -N4 -tu4 < /dev/urandom'.
      random_seed=2101079370,
      sequencing_type=deepvariant_pb2.PileupImageOptions.UNSPECIFIED_SEQ_TYPE,
      alt_aligned_pileup='')


def _compute_half_width(width):
  return int((width - 1) / 2)


def _represent_alt_aligned_pileups(representation, ref_image, alt_images):
  """Combines ref and alt-aligned pileup images according to the representation.

  Args:
    representation: string, one of "rows", "base_channels", "diff_channels".
    ref_image: 3D numpy array. The original pileup image.
    alt_images: list of either one or two 3D numpy arrays, both of the same
      dimensions as ref_image. Pileup image(s) of the same reads aligned to the
      alternate haplotype(s).

  Returns:
    One 3D numpy array containing a selection of data from the input arrays.
  """

  # If there is only one alt, duplicate it to make all pileups the same size.
  if len(alt_images) == 1:
    alt_images = alt_images + alt_images
  if len(alt_images) != 2:
    raise ValueError('alt_images must contain exactly one or two arrays.')

  # Ensure that all three pileups have the same shape.
  if not ref_image.shape == alt_images[0].shape == alt_images[1].shape:
    raise ValueError('Pileup images must be the same shape to be combined.')

  if representation == 'rows':
    # Combine all images: [ref, alt1, alt2].
    return np.concatenate([ref_image] + alt_images, axis=0)
  elif representation == 'base_channels':
    channels = [ref_image[:, :, c] for c in range(ref_image.shape[2])]
    # Add channel 0 (bases ATCG) of both alts as channels.
    channels.append(alt_images[0][:, :, 0])
    channels.append(alt_images[1][:, :, 0])
    return np.stack(channels, axis=2)
  elif representation == 'diff_channels':
    channels = [ref_image[:, :, c] for c in range(ref_image.shape[2])]
    # Add channel 5 (base differs from ref) of both alts as channels.
    channels.append(alt_images[0][:, :, 5])
    channels.append(alt_images[1][:, :, 5])
    return np.stack(channels, axis=2)
  else:
    raise ValueError(
        'alt_aligned_pileups received invalid value: "{}". Must be one of '
        'rows, base_channels, or diff_channels.'.format(representation))


class SampleType(enum.Enum):
  """Enum specifying whether sample is from child or parent."""
  CHILD = 0
  PARENT = 1


class PileupImageCreator(object):
  """High-level API for creating images of pileups of reads and reference bases.

  This class provides a higher-level and more natural API for constructing
  images at a candidate variant call site. Given a DeepVariantCall, which
  contains the candidate variant call along with key supplementary information,
  this class provides create_pileup_images() that will do all of the necessary
  fetching of reads and reference bases from readers and pass those off to the
  lower-level PileupImageEncoder to construct the image Tensor.

  for dv_call in candidates:
    allele_and_images = pic.create_pileup_images(dv_call)
    ...

  A quick note on how we deal with multiple alt alleles:

  Suppose variant has ref and two alt alleles. Assuming the sample is diploid,
  we have the following six possible genotypes:

    ref/ref   => 0/0
    ref/alt1  => 0/1
    alt1/alt1 => 1/1
    ref/alt2  => 0/2
    alt1/alt2 => 1/2
    alt2/alt2 => 2/2

  In DeepTrio we predict the genotype count (0, 1, 2) for a specific set of
  alternate alleles. If we only had a single alt, we'd construct an image for
  ref vs. alt1:

    image1 => ref vs. alt1 => determine if we are 0/0, 0/1, 1/1

  If we add a second image for alt2, we get:

    image2 => ref vs. alt2 => determine if we are 0/0, 0/2, 2/2

  but the problem here is that we don't have a good estimate for the het-alt
  state 1/2. So we construct a third image contrasting ref vs. either alt1 or
  alt2:

    image3 => ref vs. alt1 or alt2 => determines 0/0, 0/{1,2}, {1,2}/{1,2}

  Given the predictions for each image:

    image1 => p00, p01, p11
    image2 => p00, p02, p22
    image3 => p00, p0x, pxx where x is {1,2}

  we calculate our six genotype likelihoods as:

    0/0 => p00 [from any image]
    0/1 => p01 [image1]
    1/1 => p11 [image1]
    0/2 => p02 [image2]
    2/2 => p22 [image2]
    1/2 => pxx [image3]

  The function create_pileup_images() returns all of the necessary images, along
  with the alt alleles used for each image.
  """

  def __init__(self,
               options,
               ref_reader,
               sam_reader,
               sam_reader_parent1=None,
               sam_reader_parent2=None):
    self._options = options
    self._encoder = pileup_image_native.PileupImageEncoderNative(self._options)
    self._ref_reader = ref_reader
    self._sam_reader = sam_reader
    self._sam_reader_parent1 = sam_reader_parent1
    self._sam_reader_parent2 = sam_reader_parent2

  def __getattr__(self, attr):
    """Gets attributes from self._options as though they are our attributes."""
    return self._options.__getattribute__(attr)

  @property
  def half_width(self):
    return _compute_half_width(self._options.width)

  def get_reads(self, variant, sam_reader=None, parent=None):
    """Gets the reads used to construct the pileup image around variant.

    Args:
      variant: A third_party.nucleus.protos.Variant proto describing the variant
        we are creating the pileup image of.
      sam_reader: Nucleus sam_reader that allows to query reads from input BAM.
        Defaults to reading the child bam. This takes precedence over 'parent'.
      parent: None for child (default), 1 for parent1, 2 for parent2.
    Returns:
      A list of third_party.nucleus.protos.Read protos.
    """
    if not sam_reader:
      if parent is None:
        sam_reader = self._sam_reader  # child
      elif parent == 1:
        sam_reader = self._sam_reader_parent1
      elif parent == 2:
        sam_reader = self._sam_reader_parent2
      else:
        raise ValueError('parent must be None (for child), 1, or 2. '
                         'Found parent={}'.format(parent))

    query_start = variant.start - self._options.read_overlap_buffer_bp
    query_end = variant.end + self._options.read_overlap_buffer_bp
    region = ranges.make_range(variant.reference_name, query_start, query_end)
    return list(sam_reader.query(region))

  def get_reference_bases(self, variant):
    """Gets the reference bases used to make the pileup image around variant.

    Args:
      variant: A third_party.nucleus.protos.Variant proto describing the variant
        we are creating the pileup image of.

    Returns:
      A string of reference bases or None. Returns None if the reference
      interval for variant isn't valid for some reason.
    """
    start = variant.start - self.half_width
    end = start + self._options.width
    region = ranges.make_range(variant.reference_name, start, end)
    if self._ref_reader.is_valid(region):
      return self._ref_reader.query(region)
    else:
      return None

  def _alt_allele_combinations(self, variant):
    """Yields the set of all alt_alleles for variant.

    This function computes the sets of alt_alleles we want to use to cover all
    genotype likelihood calculations we need for n alt alleles (see class docs
    for background). The easiest way to do this is to calculate all combinations
    of 2 alleles from ref + alts and then strip away the reference alleles,
    leaving us with the set of alts for the pileup image encoder. The sets are
    converted to sorted lists at the end for downstream consistency.

    Args:
      variant: third_party.nucleus.protos.Variant to generate the alt allele
        combinations for.

    Yields:
      A series of lists containing the alt alleles we want to use for a single
      pileup image. The entire series covers all combinations of alt alleles
      needed for variant.

    Raises:
      ValueError: if options.multi_allelic_mode is UNSPECIFIED.
    """
    ref = variant.reference_bases
    alts = list(variant.alternate_bases)
    if (self.multi_allelic_mode ==
        deepvariant_pb2.PileupImageOptions.UNSPECIFIED):
      raise ValueError('multi_allelic_mode cannot be UNSPECIFIED')
    elif (self.multi_allelic_mode ==
          deepvariant_pb2.PileupImageOptions.NO_HET_ALT_IMAGES):
      for alt in alts:
        yield sorted([alt])
    else:
      for combination in itertools.combinations([ref] + alts, 2):
        yield sorted(list(set(combination) - {ref}))

  def build_pileup(self,
                   dv_call,
                   refbases,
                   reads,
                   alt_alleles,
                   reads_parent1=None,
                   reads_parent2=None,
                   custom_ref=False):
    """Creates a pileup tensor for dv_call.

    Args:
      dv_call: learning.genomics.deepvariant.DeepVariantCall object with
        information on our candidate call and allele support information.
      refbases: A string options.width in length containing the reference base
        sequence to encode. The middle base of this string should be at the
        start of the variant in dv_call.
      reads: Iterable of third_party.nucleus.protos.Read objects that we'll use
        to encode the child's read information supporting our call. Assumes each
        read is aligned and is well-formed (e.g., has bases and quality scores,
        cigar). Rows of the image are encoded in the same order as reads.
      alt_alleles: A collection of alternative_bases from dv_call.variant that
        we are treating as "alt" when constructing this pileup image. A read
        will be considered supporting the "alt" allele if it occurs in the
        support list for any alt_allele in this collection.
      reads_parent1: Iterable of third_party.nucleus.protos.Read objects that
        we'll use to encode the parent_1's read information supporting our call.
        Assumes each read is aligned and is well-formed (e.g., has bases and
        quality scores, cigar). Rows of the image are encoded in the same order
        as reads.
      reads_parent2: Iterable of third_party.nucleus.protos.Read objects that
        we'll use to encode the parent_2's read information supporting our call.
        Assumes each read is aligned and is well-formed (e.g., has bases and
        quality scores, cigar). Rows of the image are encoded in the same order
        as reads.
      custom_ref: True if refbases should not be checked for matching against
        variant's reference_bases.

    Returns:
      A [self.width, self.height, DEFAULT_NUM_CHANNEL] uint8 Tensor image.

    Raises:
      ValueError: if any arguments are invalid.
    """
    if len(refbases) != self.width:
      raise ValueError('refbases is {} long but width is {}'.format(
          len(refbases), self.width))

    if not alt_alleles:
      raise ValueError('alt_alleles cannot be empty')
    if any(alt not in dv_call.variant.alternate_bases for alt in alt_alleles):
      raise ValueError(
          'all elements of alt_alleles must be the alternate bases'
          ' of dv_call.variant', alt_alleles, dv_call.variant)

    image_start_pos = dv_call.variant.start - self.half_width
    if not custom_ref and (refbases[self.half_width] !=
                           dv_call.variant.reference_bases[0]):
      raise ValueError('The middle base of reference sequence in the window '
                       "({} at base {}) doesn't match first "
                       'character of variant.reference_bases ({}).'.format(
                           refbases[self.half_width], self.half_width,
                           dv_call.variant.reference_bases))

    def build_pileup_for_one_sample(reads, sample_type=None):
      """Helper function to create a section of pileup image."""

      if reads is None:
        return []
      # We start with n copies of our encoded reference bases.
      rows = ([self._encoder.encode_reference(refbases)] *
              self.reference_band_height)

      # A generator that yields tuples of the form (haplotype, position, row),
      # if the read can be encoded as a valid row to be used in the pileup
      # image.
      def _row_generator():
        """A generator that yields tuples of the form (haplotype, position, row)."""
        for read in reads:
          read_row = self._encoder.encode_read(dv_call, refbases, read,
                                               image_start_pos, alt_alleles)
          if read_row is not None:
            hap_idx = 0
            if self._options.sort_by_haplotypes:
              if 'HP' in read.info and next(iter(
                  read.info.get('HP').values)).HasField('int_value'):
                hap_idx = next(iter(read.info.get('HP').values)).int_value
            yield hap_idx, read.alignment.position.position, read_row

      # We add a row for each read in order, down-sampling if the number of
      # reads is greater than self.max_reads. Sort the reads by their alignment
      # position.
      random_for_image = np.random.RandomState(self._options.random_seed)
      max_reads_one_sample = self.height - self.reference_band_height
      if self.sequencing_type == deepvariant_pb2.PileupImageOptions.TRIO:
        if sample_type == SampleType.CHILD:
          max_reads_one_sample = self.height_child - self.reference_band_height
        elif sample_type == SampleType.PARENT:
          max_reads_one_sample = self.height_parent - self.reference_band_height
      sample = sorted(
          utils.reservoir_sample(
              _row_generator(), max_reads_one_sample, random=random_for_image),
          key=lambda x: (x[0], x[1]))

      rows += [read_row for _, _, read_row in sample]

      # Finally, fill in any missing rows to bring our image to self.height rows
      # with empty (all black) pixels.
      height_one_sample = self.height
      if self.sequencing_type == deepvariant_pb2.PileupImageOptions.TRIO:
        if sample_type == SampleType.CHILD:
          height_one_sample = self.height_child
        elif sample_type == SampleType.PARENT:
          height_one_sample = self.height_parent
      n_missing_rows = height_one_sample - len(rows)
      if n_missing_rows > 0:
        # Add values to rows to fill it out with zeros.
        rows += [self._empty_image_row()] * n_missing_rows

      return rows

    # Build rows. Optionally 3 pile ups are merged together for trio. In case
    # of trio child has to be in the middle.
    rows = []
    if self.sequencing_type == deepvariant_pb2.PileupImageOptions.TRIO:
      rows.extend(
          build_pileup_for_one_sample(
              reads_parent1, sample_type=SampleType.PARENT))

    rows.extend(
        build_pileup_for_one_sample(reads, sample_type=SampleType.CHILD))

    if self.sequencing_type == deepvariant_pb2.PileupImageOptions.TRIO:
      rows.extend(
          build_pileup_for_one_sample(
              reads_parent2, sample_type=SampleType.PARENT))

    # Vertically stack the image rows to create a single
    # h x w x DEFAULT_NUM_CHANNEL image.
    return np.vstack(rows)

  def _empty_image_row(self):
    """Creates an empty image row as an uint8 np.array."""
    return np.zeros((1, self.width, self.num_channels), dtype=np.uint8)

  def create_pileup_images(self,
                           dv_call,
                           haplotype_alignments=None,
                           haplotype_sequences=None,
                           parent1_hap_alns=None,
                           parent2_hap_alns=None):
    """Creates a DeepTrio TF.Example for the DeepVariant call dv_call.

    See class documents for more details.

    Args:
      dv_call: A learning.genomics.deepvariant.DeepVariantCall proto that we
        want to create a TF.Example pileup image of.
      haplotype_alignments: dict of read alignments keyed by haplotype, for
        child.
      haplotype_sequences: dict of sequences keyed by haplotype, for
        child.
      parent1_hap_alns: same as haplotype_alignments but for parent 1.
      parent2_hap_alns: same as haplotype_alignments but for parent 2.

    Returns:
      A list of tuples. The first element of the tuple is a set of alternate
      alleles used as 'alt' when encoding this image. The second element is a
      [w, h, DEFAULT_NUM_CHANNEL] uint8 Tensor of the pileup image for those
      alt alleles.
    """
    variant = dv_call.variant
    # Ref bases to show at the top of the pileup:
    ref_bases = self.get_reference_bases(variant)
    if not ref_bases:
      # This interval isn't valid => we are off the edge of the chromosome, so
      # return None to indicate we couldn't process this variant.
      return None

    reads = self.get_reads(variant)
    reads_parent1 = self.get_reads(variant, parent=1)
    reads_parent2 = self.get_reads(variant, parent=2)

    alt_aligned_representation = self._options.alt_aligned_pileup

    def _pileup_for_pair_of_alts(alt_alleles):
      """Create pileup image for one combination of alt alleles."""
      # Always create the ref-aligned pileup image.
      ref_image = self.build_pileup(
          dv_call=dv_call,
          refbases=ref_bases,
          reads=reads,
          alt_alleles=alt_alleles,
          reads_parent1=reads_parent1,
          reads_parent2=reads_parent2)
      # Optionally also create pileup images with reads aligned to alts.
      if alt_aligned_representation:
        if not (haplotype_alignments and haplotype_sequences and
                parent1_hap_alns and parent2_hap_alns):
          raise ValueError(
              'haplotype_alignments, parent1_hap_alns, parent2_hap_alns, and '
              'haplotype_sequences must all be populated if '
              'alt_aligned_pileups is turned on.')
        # pylint: disable=g-complex-comprehension
        alt_images = [
            self.build_pileup(
                dv_call=dv_call,
                refbases=haplotype_sequences[alt],
                reads=haplotype_alignments[alt],
                alt_alleles=alt_alleles,
                reads_parent1=parent1_hap_alns[alt],
                reads_parent2=parent2_hap_alns[alt],
                custom_ref=True) for alt in alt_alleles
        ]
        # pylint: enable=g-complex-comprehension
        return _represent_alt_aligned_pileups(alt_aligned_representation,
                                              ref_image, alt_images)
      else:
        return ref_image

    return [(alts, _pileup_for_pair_of_alts(alts))
            for alts in self._alt_allele_combinations(variant)]