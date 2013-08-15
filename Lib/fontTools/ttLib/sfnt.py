"""ttLib/sfnt.py -- low-level module to deal with the sfnt file format.

Defines two public classes:
	SFNTReader
	SFNTWriter

(Normally you don't have to use these classes explicitly; they are 
used automatically by ttLib.TTFont.)

The reading and writing of sfnt files is separated in two distinct 
classes, since whenever to number of tables changes or whenever
a table's length chages you need to rewrite the whole file anyway.
"""

import sys
import struct, sstruct
import os


class SFNTReader:
	
	def __init__(self, file, checkChecksums=1, fontNumber=-1):
		self.file = file
		self.checkChecksums = checkChecksums

		self.flavor = None
		self.flavorData = None
		self.DirectoryEntry = SFNTDirectoryEntry
		self.sfntVersion = self.file.read(4)
		self.file.seek(0)
		if self.sfntVersion == "ttcf":
			sstruct.unpack(ttcHeaderFormat, self.file.read(ttcHeaderSize), self)
			assert self.Version == 0x00010000 or self.Version == 0x00020000, "unrecognized TTC version 0x%08x" % self.Version
			if not 0 <= fontNumber < self.numFonts:
				from fontTools import ttLib
				raise ttLib.TTLibError, "specify a font number between 0 and %d (inclusive)" % (self.numFonts - 1)
			offsetTable = struct.unpack(">%dL" % self.numFonts, self.file.read(self.numFonts * 4))
			if self.Version == 0x00020000:
				pass # ignoring version 2.0 signatures
			self.file.seek(offsetTable[fontNumber])
			sstruct.unpack(sfntDirectoryFormat, self.file.read(sfntDirectorySize), self)
		elif self.sfntVersion == "wOFF":
			self.flavor = "woff"
			self.DirectoryEntry = WOFFDirectoryEntry
			sstruct.unpack(woffDirectoryFormat, self.file.read(woffDirectorySize), self)
		else:
			sstruct.unpack(sfntDirectoryFormat, self.file.read(sfntDirectorySize), self)

		if self.sfntVersion not in ("\000\001\000\000", "OTTO", "true"):
			from fontTools import ttLib
			raise ttLib.TTLibError, "Not a TrueType or OpenType font (bad sfntVersion)"
		self.tables = {}
		for i in range(self.numTables):
			entry = self.DirectoryEntry()
			entry.fromFile(self.file)
			if entry.length > 0:
				self.tables[entry.tag] = entry
			else:
				# Ignore zero-length tables. This doesn't seem to be documented,
				# yet it's apparently how the Windows TT rasterizer behaves.
				# Besides, at least one font has been sighted which actually
				# *has* a zero-length table.
				pass

		# Load flavor data if any
		if self.flavor == "woff":
			self.flavorData = WOFFFlavorData(self)

	def has_key(self, tag):
		return self.tables.has_key(tag)
	
	def keys(self):
		return self.tables.keys()
	
	def __getitem__(self, tag):
		"""Fetch the raw table data."""
		entry = self.tables[tag]
		data = entry.loadData (self.file)
		if self.checkChecksums:
			if tag == 'head':
				# Beh: we have to special-case the 'head' table.
				checksum = calcChecksum(data[:8] + '\0\0\0\0' + data[12:])
			else:
				checksum = calcChecksum(data)
			if self.checkChecksums > 1:
				# Be obnoxious, and barf when it's wrong
				assert checksum == entry.checksum, "bad checksum for '%s' table" % tag
			elif checksum <> entry.checkSum:
				# Be friendly, and just print a warning.
				print "bad checksum for '%s' table" % tag
		return data
	
	def __delitem__(self, tag):
		del self.tables[tag]
	
	def close(self):
		self.file.close()


class SFNTWriter:
	
	def __init__(self, file, numTables, sfntVersion="\000\001\000\000"):
		self.file = file
		self.numTables = numTables
		self.sfntVersion = sfntVersion
		self.searchRange, self.entrySelector, self.rangeShift = getSearchRange(numTables)
		self.nextTableOffset = sfntDirectorySize + numTables * sfntDirectoryEntrySize
		# clear out directory area
		self.file.seek(self.nextTableOffset)
		# make sure we're actually where we want to be. (XXX old cStringIO bug)
		self.file.write('\0' * (self.nextTableOffset - self.file.tell()))
		self.tables = {}
	
	def __setitem__(self, tag, data):
		"""Write raw table data to disk."""
		if self.tables.has_key(tag):
			# We've written this table to file before. If the length
			# of the data is still the same, we allow overwriting it.
			entry = self.tables[tag]
			if len(data) <> entry.length:
				from fontTools import ttLib
				raise ttLib.TTLibError, "cannot rewrite '%s' table: length does not match directory entry" % tag
		else:
			entry = SFNTDirectoryEntry()
			entry.tag = tag
			entry.offset = self.nextTableOffset
			entry.length = len(data)
			self.nextTableOffset = self.nextTableOffset + ((len(data) + 3) & ~3)
		self.file.seek(entry.offset)
		self.file.write(data)
		# Add NUL bytes to pad the table data to a 4-byte boundary.
		# Don't depend on f.seek() as we need to add the padding even if no
		# subsequent write follows (seek is lazy), ie. after the final table
		# in the font.
		self.file.write('\0' * (self.nextTableOffset - self.file.tell()))
		assert self.nextTableOffset == self.file.tell()
		
		if tag == 'head':
			entry.checkSum = calcChecksum(data[:8] + '\0\0\0\0' + data[12:])
		else:
			entry.checkSum = calcChecksum(data)
		self.tables[tag] = entry
	
	def close(self):
		"""All tables must have been written to disk. Now write the
		directory.
		"""
		tables = self.tables.items()
		tables.sort()
		if len(tables) <> self.numTables:
			from fontTools import ttLib
			raise ttLib.TTLibError, "wrong number of tables; expected %d, found %d" % (self.numTables, len(tables))
		
		directory = sstruct.pack(sfntDirectoryFormat, self)
		
		self.file.seek(sfntDirectorySize)
		seenHead = 0
		for tag, entry in tables:
			if tag == "head":
				seenHead = 1
			directory = directory + entry.toString()
		if seenHead:
			self.writeMasterChecksum(directory)
		self.file.seek(0)
		self.file.write(directory)

	def _calcMasterChecksum(self, directory):
		# calculate checkSumAdjustment
		tags = self.tables.keys()
		checksums = []
		for i in range(len(tags)):
			checksums.append(self.tables[tags[i]].checkSum)

		directory_end = sfntDirectorySize + len(self.tables) * sfntDirectoryEntrySize
		assert directory_end == len(directory)

		checksums.append(calcChecksum(directory))
		checksum = sum(checksums) & 0xffffffff
		# BiboAfba!
		checksumadjustment = (0xB1B0AFBA - checksum) & 0xffffffff
		return checksumadjustment

	def writeMasterChecksum(self, directory):
		checksumadjustment = self._calcMasterChecksum(directory)
		# write the checksum to the file
		self.file.seek(self.tables['head'].offset + 8)
		self.file.write(struct.pack(">L", checksumadjustment))


# -- sfnt directory helpers and cruft

ttcHeaderFormat = """
		> # big endian
		TTCTag:                  4s # "ttcf"
		Version:                 L  # 0x00010000 or 0x00020000
		numFonts:                L  # number of fonts
		# OffsetTable[numFonts]: L  # array with offsets from beginning of file
		# ulDsigTag:             L  # version 2.0 only
		# ulDsigLength:          L  # version 2.0 only
		# ulDsigOffset:          L  # version 2.0 only
"""

ttcHeaderSize = sstruct.calcsize(ttcHeaderFormat)

sfntDirectoryFormat = """
		> # big endian
		sfntVersion:    4s
		numTables:      H    # number of tables
		searchRange:    H    # (max2 <= numTables)*16
		entrySelector:  H    # log2(max2 <= numTables)
		rangeShift:     H    # numTables*16-searchRange
"""

sfntDirectorySize = sstruct.calcsize(sfntDirectoryFormat)

sfntDirectoryEntryFormat = """
		> # big endian
		tag:            4s
		checkSum:       L
		offset:         L
		length:         L
"""

sfntDirectoryEntrySize = sstruct.calcsize(sfntDirectoryEntryFormat)

woffDirectoryFormat = """
		> # big endian
		signature:      4s   # "wOFF"
		sfntVersion:    4s
		length:         L    # total woff file size
		numTables:      H    # number of tables
		reserved:       H    # set to 0
		totalSfntSize:  L    # uncompressed size
		majorVersion:   H    # major version of WOFF file
		minorVersion:   H    # minor version of WOFF file
		metaOffset:     L    # offset to metadata block
		metaLength:     L    # length of compressed metadata
		metaOrigLength: L    # length of uncompressed metadata
		privOffset:     L    # offset to private data block
		privLength:     L    # length of private data block
"""

woffDirectorySize = sstruct.calcsize(woffDirectoryFormat)

woffDirectoryEntryFormat = """
		> # big endian
		tag:            4s
		offset:         L
		length:         L    # compressed length
		origLength:     L    # original length
		checksum:       L    # original checksum
"""

woffDirectoryEntrySize = sstruct.calcsize(woffDirectoryEntryFormat)


class DirectoryEntry:
	
	def fromFile(self, file):
		sstruct.unpack(self.format, file.read(self.formatSize), self)
	
	def fromString(self, str):
		sstruct.unpack(self.format, str, self)
	
	def toString(self):
		return sstruct.pack(self.format, self)
	
	def __repr__(self):
		if hasattr(self, "tag"):
			return "<%s '%s' at %x>" % (self.__class__.__name__, self.tag, id(self))
		else:
			return "<%s at %x>" % (self.__class__.__name__, id(self))

	def loadData(self, file):
		file.seek(self.offset)
		data = file.read(self.length)
		assert len(data) == self.length
		return self.decodeData (data)

	def decodeData(self, rawData):
		return rawData

class SFNTDirectoryEntry(DirectoryEntry):

	format = sfntDirectoryEntryFormat
	formatSize = sfntDirectoryEntrySize

class WOFFDirectoryEntry(DirectoryEntry):

	format = woffDirectoryEntryFormat
	formatSize = woffDirectoryEntrySize

	def decodeData(self, rawData):
		import zlib
		if self.length == self.origLength:
			data = rawData
		else:
			assert self.length < self.origLength
			data = zlib.decompress(rawData)
			assert len (data) == self.origLength
		return data

class WOFFFlavorData():

	def __init__(self, reader=None):
		self.majorVersion = None
		self.minorVersion = None
		self.metaData = None
		self.privData = None
		if reader:
			self.majorVersion = reader.majorVersion
			self.minorVersion = reader.minorVersion
			if reader.metaLength:
				reader.file.seek(reader.metaOffset)
				rawData = read.file.read(reader.metaLength)
				assert len(rawData) == reader.metaLength
				data = zlib.decompress(rawData)
				assert len(data) == reader.metaOrigLength
				self.metaData = data
			if reader.privLength:
				reader.file.seek(reader.privOffset)
				data = read.file.read(reader.privLength)
				assert len(data) == reader.privLength
				self.privData = data


def calcChecksum(data):
	"""Calculate the checksum for an arbitrary block of data.
	Optionally takes a 'start' argument, which allows you to
	calculate a checksum in chunks by feeding it a previous
	result.
	
	If the data length is not a multiple of four, it assumes
	it is to be padded with null byte. 

		>>> print calcChecksum("abcd")
		1633837924
		>>> print calcChecksum("abcdxyz")
		3655064932
	"""
	remainder = len(data) % 4
	if remainder:
		data += "\0" * (4 - remainder)
	value = 0
	blockSize = 4096
	assert blockSize % 4 == 0
	for i in xrange(0, len(data), blockSize):
		block = data[i:i+blockSize]
		longs = struct.unpack(">%dL" % (len(block) // 4), block)
		value = (value + sum(longs)) & 0xffffffff
	return value


def maxPowerOfTwo(x):
	"""Return the highest exponent of two, so that
	(2 ** exponent) <= x
	"""
	exponent = 0
	while x:
		x = x >> 1
		exponent = exponent + 1
	return max(exponent - 1, 0)


def getSearchRange(n):
	"""Calculate searchRange, entrySelector, rangeShift for the
	sfnt directory. 'n' is the number of tables.
	"""
	# This stuff needs to be stored in the file, because?
	import math
	exponent = maxPowerOfTwo(n)
	searchRange = (2 ** exponent) * 16
	entrySelector = exponent
	rangeShift = n * 16 - searchRange
	return searchRange, entrySelector, rangeShift


if __name__ == "__main__":
    import doctest
    doctest.testmod()
