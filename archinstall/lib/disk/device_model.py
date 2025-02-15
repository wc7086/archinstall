from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from enum import auto
from pathlib import Path
from typing import Optional, List, Dict, TYPE_CHECKING, Any
from typing import Union

import parted  # type: ignore
from parted import Disk, Geometry, Partition

from ..exceptions import DiskError, SysCallError
from ..general import SysCommand
from ..output import log
from ..storage import storage

if TYPE_CHECKING:
	_: Any


class DiskLayoutType(Enum):
	Default = 'default_layout'
	Manual = 'manual_partitioning'
	Pre_mount = 'pre_mounted_config'

	def display_msg(self) -> str:
		match self:
			case DiskLayoutType.Default: return str(_('Use a best-effort default partition layout'))
			case DiskLayoutType.Manual: return str(_('Manual Partitioning'))
			case DiskLayoutType.Pre_mount: return str(_('Pre-mounted configuration'))


@dataclass
class DiskLayoutConfiguration:
	config_type: DiskLayoutType
	device_modifications: List[DeviceModification] = field(default_factory=list)
	# used for pre-mounted config
	relative_mountpoint: Optional[Path] = None

	def __post_init__(self):
		if self.config_type == DiskLayoutType.Pre_mount and self.relative_mountpoint is None:
			raise ValueError('Must set a relative mountpoint when layout type is pre-mount"')

	def __dump__(self) -> Dict[str, Any]:
		return {
			'config_type': self.config_type.value,
			'device_modifications': [mod.__dump__() for mod in self.device_modifications]
		}

	@classmethod
	def parse_arg(cls, disk_config: Dict[str, List[Dict[str, Any]]]) -> Optional[DiskLayoutConfiguration]:
		from .device_handler import device_handler

		device_modifications: List[DeviceModification] = []
		config_type = disk_config.get('config_type', None)

		if not config_type:
			raise ValueError('Missing disk layout configuration: config_type')

		config = DiskLayoutConfiguration(
			config_type=DiskLayoutType(config_type),
			device_modifications=device_modifications
		)

		for entry in disk_config.get('device_modifications', []):
			device_path = Path(entry.get('device', None)) if entry.get('device', None) else None

			if not device_path:
				continue

			device = device_handler.get_device(device_path)

			if not device:
				continue

			device_modification = DeviceModification(
				wipe=entry.get('wipe', False),
				device=device
			)

			device_partitions: List[PartitionModification] = []

			for partition in entry.get('partitions', []):
				device_partition = PartitionModification(
					status=ModificationStatus(partition['status']),
					fs_type=FilesystemType(partition['fs_type']),
					start=Size.parse_args(partition['start']),
					length=Size.parse_args(partition['length']),
					mount_options=partition['mount_options'],
					mountpoint=Path(partition['mountpoint']) if partition['mountpoint'] else None,
					type=PartitionType(partition['type']),
					flags=[PartitionFlag[f] for f in partition.get('flags', [])],
					btrfs_subvols=SubvolumeModification.parse_args(partition.get('btrfs', [])),
				)
				# special 'invisible attr to internally identify the part mod
				setattr(device_partition, '_obj_id', partition['obj_id'])
				device_partitions.append(device_partition)

			device_modification.partitions = device_partitions
			device_modifications.append(device_modification)

		return config


class PartitionTable(Enum):
	GPT = 'gpt'
	MBR = 'msdos'


class Unit(Enum):
	B = 1          # byte
	kB = 1000**1   # kilobyte
	MB = 1000**2   # megabyte
	GB = 1000**3   # gigabyte
	TB = 1000**4   # terabyte
	PB = 1000**5   # petabyte
	EB = 1000**6   # exabyte
	ZB = 1000**7   # zettabyte
	YB = 1000**8   # yottabyte

	KiB = 1024**1 	# kibibyte
	MiB = 1024**2 	# mebibyte
	GiB = 1024**3  	# gibibyte
	TiB = 1024**4  	# tebibyte
	PiB = 1024**5  	# pebibyte
	EiB = 1024**6  	# exbibyte
	ZiB = 1024**7  	# zebibyte
	YiB = 1024**8  	# yobibyte

	sectors = 'sectors'  # size in sector

	Percent = '%' 	# size in percentile


@dataclass
class Size:
	value: int
	unit: Unit
	sector_size: Optional[Size] = None  # only required when unit is sector
	total_size: Optional[Size] = None  # required when operating on percentages

	def __post_init__(self):
		if self.unit == Unit.sectors and self.sector_size is None:
			raise ValueError('Sector size is required when unit is sectors')
		elif self.unit == Unit.Percent:
			if self.value < 0 or self.value > 100:
				raise ValueError('Percentage must be between 0 and 100')
			elif self.total_size is None:
				raise ValueError('Total size is required when unit is percentage')

	@property
	def _total_size(self) -> Size:
		"""
		Save method to get the total size, mainly to satisfy mypy
		This shouldn't happen as the Size object fails instantiation on missing total size
		"""
		if self.unit == Unit.Percent and self.total_size is None:
			raise ValueError('Percent unit size must specify a total size')
		return self.total_size  # type: ignore

	def __dump__(self) -> Dict[str, Any]:
		return {
			'value': self.value,
			'unit': self.unit.name,
			'sector_size': self.sector_size.__dump__() if self.sector_size else None,
			'total_size': self._total_size.__dump__() if self._total_size else None
		}

	@classmethod
	def parse_args(cls, size_arg: Dict[str, Any]) -> Size:
		sector_size = size_arg['sector_size']
		total_size = size_arg['total_size']

		return Size(
			size_arg['value'],
			Unit[size_arg['unit']],
			Size.parse_args(sector_size) if sector_size else None,
			Size.parse_args(total_size) if total_size else None
		)

	def convert(
		self,
		target_unit: Unit,
		sector_size: Optional[Size] = None,
		total_size: Optional[Size] = None
	) -> Size:
		if target_unit == Unit.sectors and sector_size is None:
			raise ValueError('If target has unit sector, a sector size must be provided')

		# not sure why we would ever wanna convert to percentages
		if target_unit == Unit.Percent and total_size is None:
			raise ValueError('Missing paramter total size to be able to convert to percentage')

		if self.unit == target_unit:
			return self
		elif self.unit == Unit.Percent:
			amount = int(self._total_size._normalize() * (self.value / 100))
			return Size(amount, Unit.B)
		elif self.unit == Unit.sectors:
			norm = self._normalize()
			return Size(norm, Unit.B).convert(target_unit, sector_size)
		else:
			if target_unit == Unit.sectors and sector_size is not None:
				norm = self._normalize()
				sectors = math.ceil(norm / sector_size.value)
				return Size(sectors, Unit.sectors, sector_size)
			else:
				value = int(self._normalize() / target_unit.value)  # type: ignore
				return Size(value, target_unit)

	def format_size(
		self,
		target_unit: Unit,
		sector_size: Optional[Size] = None
	) -> str:
		if self.unit == Unit.Percent:
			return f'{self.value}%'
		else:
			target_size = self.convert(target_unit, sector_size)
			return f'{target_size.value} {target_unit.name}'

	def _normalize(self) -> int:
		"""
		will normalize the value of the unit to Byte
		"""
		if self.unit == Unit.Percent:
			return self.convert(Unit.B).value
		elif self.unit == Unit.sectors and self.sector_size is not None:
			return self.value * self.sector_size._normalize()
		return int(self.value * self.unit.value)  # type: ignore

	def __sub__(self, other: Size) -> Size:
		src_norm = self._normalize()
		dest_norm = other._normalize()
		return Size(abs(src_norm - dest_norm), Unit.B)

	def __lt__(self, other):
		return self._normalize() < other._normalize()

	def __le__(self, other):
		return self._normalize() <= other._normalize()

	def __eq__(self, other):
		return self._normalize() == other._normalize()

	def __ne__(self, other):
		return self._normalize() != other._normalize()

	def __gt__(self, other):
		return self._normalize() > other._normalize()

	def __ge__(self, other):
		return self._normalize() >= other._normalize()


@dataclass
class _BtrfsSubvolumeInfo:
	name: Path
	mountpoint: Optional[Path]


@dataclass
class _PartitionInfo:
	partition: Partition
	name: str
	type: PartitionType
	fs_type: FilesystemType
	path: Path
	start: Size
	length: Size
	flags: List[PartitionFlag]
	partuuid: str
	disk: Disk
	mountpoints: List[Path]
	btrfs_subvol_infos: List[_BtrfsSubvolumeInfo] = field(default_factory=list)

	def as_json(self) -> Dict[str, Any]:
		info = {
			'Name': self.name,
			'Type': self.type.value,
			'Filesystem': self.fs_type.value if self.fs_type else str(_('Unknown')),
			'Path': str(self.path),
			'Start': self.start.format_size(Unit.MiB),
			'Length': self.length.format_size(Unit.MiB),
			'Flags': ', '.join([f.name for f in self.flags])
		}

		if self.btrfs_subvol_infos:
			info['Btrfs vol.'] = f'{len(self.btrfs_subvol_infos)} subvolumes'

		return info

	@classmethod
	def from_partition(
		cls,
		partition: Partition,
		fs_type: FilesystemType,
		partuuid: str,
		mountpoints: List[Path],
		btrfs_subvol_infos: List[_BtrfsSubvolumeInfo] = []
	) -> _PartitionInfo:
		partition_type = PartitionType.get_type_from_code(partition.type)
		flags = [f for f in PartitionFlag if partition.getFlag(f.value)]

		start = Size(
			partition.geometry.start,
			Unit.sectors,
			Size(partition.disk.device.sectorSize, Unit.B)
		)

		length = Size(int(partition.getLength(unit='B')), Unit.B)

		return _PartitionInfo(
			partition=partition,
			name=partition.get_name(),
			type=partition_type,
			fs_type=fs_type,
			path=partition.path,
			start=start,
			length=length,
			flags=flags,
			partuuid=partuuid,
			disk=partition.disk,
			mountpoints=mountpoints,
			btrfs_subvol_infos=btrfs_subvol_infos
		)


@dataclass
class _DeviceInfo:
	model: str
	path: Path
	type: str
	total_size: Size
	free_space_regions: List[DeviceGeometry]
	sector_size: Size
	read_only: bool
	dirty: bool

	def as_json(self) -> Dict[str, Any]:
		total_free_space = sum([region.get_length(unit=Unit.MiB) for region in self.free_space_regions])
		return {
			'Model': self.model,
			'Path': str(self.path),
			'Type': self.type,
			'Size': self.total_size.format_size(Unit.MiB),
			'Free space': int(total_free_space),
			'Sector size': self.sector_size.value,
			'Read only': self.read_only
		}

	@classmethod
	def from_disk(cls, disk: Disk) -> _DeviceInfo:
		device = disk.device
		device_type = parted.devices[device.type]

		sector_size = Size(device.sectorSize, Unit.B)
		free_space = [DeviceGeometry(g, sector_size) for g in disk.getFreeSpaceRegions()]

		return _DeviceInfo(
			model=device.model.strip(),
			path=Path(device.path),
			type=device_type,
			sector_size=sector_size,
			total_size=Size(int(device.getLength(unit='B')), Unit.B),
			free_space_regions=free_space,
			read_only=device.readOnly,
			dirty=device.dirty
		)


@dataclass
class SubvolumeModification:
	name: Path
	mountpoint: Optional[Path] = None
	compress: bool = False
	nodatacow: bool = False

	@classmethod
	def from_existing_subvol_info(cls, info: _BtrfsSubvolumeInfo) -> SubvolumeModification:
		return SubvolumeModification(info.name, mountpoint=info.mountpoint)

	@classmethod
	def parse_args(cls, subvol_args: List[Dict[str, Any]]) -> List[SubvolumeModification]:
		mods = []
		for entry in subvol_args:
			if not entry.get('name', None) or not entry.get('mountpoint', None):
				log(f'Subvolume arg is missing name: {entry}', level=logging.DEBUG)
				continue

			mountpoint = Path(entry['mountpoint']) if entry['mountpoint'] else None

			mods.append(
				SubvolumeModification(
					entry['name'],
					mountpoint,
					entry.get('compress', False),
					entry.get('nodatacow', False)
				)
			)

		return mods

	@property
	def mount_options(self) -> List[str]:
		options = []
		options += ['compress'] if self.compress else []
		options += ['nodatacow'] if self.nodatacow else []
		return options

	@property
	def relative_mountpoint(self) -> Path:
		"""
		Will return the relative path based on the anchor
		e.g. Path('/mnt/test') -> Path('mnt/test')
		"""
		if self.mountpoint is not None:
			return self.mountpoint.relative_to(self.mountpoint.anchor)

		raise ValueError('Mountpoint is not specified')

	def is_root(self, relative_mountpoint: Optional[Path] = None) -> bool:
		if self.mountpoint:
			if relative_mountpoint is not None:
				return self.mountpoint.relative_to(relative_mountpoint) == Path('.')
			return self.mountpoint == Path('/')
		return False

	def __dump__(self) -> Dict[str, Any]:
		return {
			'name': str(self.name),
			'mountpoint': str(self.mountpoint),
			'compress': self.compress,
			'nodatacow': self.nodatacow
		}

	def as_json(self) -> Dict[str, Any]:
		return {
			'name': str(self.name),
			'mountpoint': str(self.mountpoint),
			'compress': self.compress,
			'nodatacow': self.nodatacow
		}


class DeviceGeometry:
	def __init__(self, geometry: Geometry, sector_size: Size):
		self._geometry = geometry
		self._sector_size = sector_size

	@property
	def start(self) -> int:
		return self._geometry.start

	@property
	def end(self) -> int:
		return self._geometry.end

	def get_length(self, unit: Unit = Unit.sectors) -> int:
		return self._geometry.getLength(unit.name)

	def as_json(self) -> Dict[str, Any]:
		return {
			'Sector size': self._sector_size.value,
			'Start sector': self._geometry.start,
			'End sector': self._geometry.end,
			'Length': self._geometry.getLength()
		}


@dataclass
class BDevice:
	disk: Disk
	device_info: _DeviceInfo
	partition_infos: List[_PartitionInfo]

	def __hash__(self):
		return hash(self.disk.device.path)


class PartitionType(Enum):
	Boot = 'boot'
	Primary = 'primary'

	@classmethod
	def get_type_from_code(cls, code: int) -> PartitionType:
		if code == parted.PARTITION_NORMAL:
			return PartitionType.Primary

		raise DiskError(f'Partition code not supported: {code}')

	def get_partition_code(self) -> Optional[int]:
		if self == PartitionType.Primary:
			return parted.PARTITION_NORMAL
		elif self == PartitionType.Boot:
			return parted.PARTITION_BOOT
		return None


class PartitionFlag(Enum):
	Boot = 1


class FilesystemType(Enum):
	Btrfs = 'btrfs'
	Ext2 = 'ext2'
	Ext3 = 'ext3'
	Ext4 = 'ext4'
	F2fs = 'f2fs'
	Fat16 = 'fat16'
	Fat32 = 'fat32'
	Ntfs = 'ntfs'
	Reiserfs = 'reiserfs'
	Xfs = 'xfs'

	# this is not a FS known to parted, so be careful
	# with the usage from this enum
	Crypto_luks = 'crypto_LUKS'

	def is_crypto(self) -> bool:
		return self == FilesystemType.Crypto_luks

	@property
	def fs_type_mount(self) -> str:
		match self:
			case FilesystemType.Ntfs: return 'ntfs3'
			case FilesystemType.Fat32: return 'vfat'
			case _: return self.value  # type: ignore

	@property
	def installation_pkg(self) -> Optional[str]:
		match self:
			case FilesystemType.Btrfs: return 'btrfs-progs'
			case FilesystemType.Xfs: return 'xfsprogs'
			case FilesystemType.F2fs: return 'f2fs-tools'
			case _: return None

	@property
	def installation_module(self) -> Optional[str]:
		match self:
			case FilesystemType.Btrfs: return 'btrfs'
			case _: return None

	@property
	def installation_binary(self) -> Optional[str]:
		match self:
			case FilesystemType.Btrfs: return '/usr/bin/btrfs'
			case _: return None

	@property
	def installation_hooks(self) -> Optional[str]:
		match self:
			case FilesystemType.Btrfs: return 'btrfs'
			case _: return None


class ModificationStatus(Enum):
	Exist = 'existing'
	Modify = 'modify'
	Delete = 'delete'
	Create = 'create'


@dataclass
class PartitionModification:
	status: ModificationStatus
	type: PartitionType
	start: Size
	length: Size
	fs_type: FilesystemType
	mountpoint: Optional[Path] = None
	mount_options: List[str] = field(default_factory=list)
	flags: List[PartitionFlag] = field(default_factory=list)
	btrfs_subvols: List[SubvolumeModification] = field(default_factory=list)

	# only set if the device was created or exists
	dev_path: Optional[Path] = None
	partuuid: Optional[str] = None
	uuid: Optional[str] = None

	def __post_init__(self):
		# needed to use the object as a dictionary key due to hash func
		if not hasattr(self, '_obj_id'):
			self._obj_id = uuid.uuid4()

		if self.is_exists_or_modify() and not self.dev_path:
			raise ValueError('If partition marked as existing a path must be set')

	def __hash__(self):
		return hash(self._obj_id)

	@property
	def obj_id(self) -> str:
		if hasattr(self, '_obj_id'):
			return str(self._obj_id)
		return ''

	@property
	def real_dev_path(self) -> Path:
		if self.dev_path is None:
			raise ValueError('Device path was not set')
		return self.dev_path

	@classmethod
	def from_existing_partition(cls, partition_info: _PartitionInfo) -> PartitionModification:
		if partition_info.btrfs_subvol_infos:
			mountpoint = None
			subvol_mods = []
			for info in partition_info.btrfs_subvol_infos:
				subvol_mods.append(
					SubvolumeModification.from_existing_subvol_info(info)
				)
		else:
			mountpoint = partition_info.mountpoints[0] if partition_info.mountpoints else None
			subvol_mods = []

		return PartitionModification(
			status=ModificationStatus.Exist,
			type=partition_info.type,
			start=partition_info.start,
			length=partition_info.length,
			fs_type=partition_info.fs_type,
			dev_path=partition_info.path,
			flags=partition_info.flags,
			mountpoint=mountpoint,
			btrfs_subvols=subvol_mods
		)

	@property
	def relative_mountpoint(self) -> Path:
		"""
		Will return the relative path based on the anchor
		e.g. Path('/mnt/test') -> Path('mnt/test')
		"""
		if self.mountpoint:
			return self.mountpoint.relative_to(self.mountpoint.anchor)

		raise ValueError('Mountpoint is not specified')

	def is_boot(self) -> bool:
		return PartitionFlag.Boot in self.flags

	def is_root(self, relative_mountpoint: Optional[Path] = None) -> bool:
		if relative_mountpoint is not None and self.mountpoint is not None:
			return self.mountpoint.relative_to(relative_mountpoint) == Path('.')
		elif self.mountpoint is not None:
			return Path('/') == self.mountpoint
		else:
			for subvol in self.btrfs_subvols:
				if subvol.is_root(relative_mountpoint):
					return True

		return False

	def is_modify(self) -> bool:
		return self.status == ModificationStatus.Modify

	def exists(self) -> bool:
		return self.status == ModificationStatus.Exist

	def is_exists_or_modify(self) -> bool:
		return self.status in [ModificationStatus.Exist, ModificationStatus.Modify]

	@property
	def mapper_name(self) -> Optional[str]:
		if self.dev_path:
			return f'{storage.get("ENC_IDENTIFIER", "ai")}{self.dev_path.name}'
		return None

	def set_flag(self, flag: PartitionFlag):
		if flag not in self.flags:
			self.flags.append(flag)

	def invert_flag(self, flag: PartitionFlag):
		if flag in self.flags:
			self.flags = [f for f in self.flags if f != flag]
		else:
			self.set_flag(flag)

	def json(self) -> Dict[str, Any]:
		"""
		Called for configuration settings
		"""
		return {
			'obj_id': self.obj_id,
			'status': self.status.value,
			'type': self.type.value,
			'start': self.start.__dump__(),
			'length': self.length.__dump__(),
			'fs_type': self.fs_type.value,
			'mountpoint': str(self.mountpoint) if self.mountpoint else None,
			'mount_options': self.mount_options,
			'flags': [f.name for f in self.flags],
			'btrfs': [vol.__dump__() for vol in self.btrfs_subvols]
		}

	def as_json(self) -> Dict[str, Any]:
		"""
		Called for displaying data in table format
		"""
		info = {
			'Status': self.status.value,
			'Device': str(self.dev_path) if self.dev_path else '',
			'Type': self.type.value,
			'Start': self.start.format_size(Unit.MiB),
			'Length': self.length.format_size(Unit.MiB),
			'FS type': self.fs_type.value,
			'Mountpoint': self.mountpoint if self.mountpoint else '',
			'Mount options': ', '.join(self.mount_options),
			'Flags': ', '.join([f.name for f in self.flags]),
		}

		if self.btrfs_subvols:
			info['Btrfs vol.'] = f'{len(self.btrfs_subvols)} subvolumes'

		return info


@dataclass
class DeviceModification:
	device: BDevice
	wipe: bool
	partitions: List[PartitionModification] = field(default_factory=list)

	@property
	def device_path(self) -> Path:
		return self.device.device_info.path

	def add_partition(self, partition: PartitionModification):
		self.partitions.append(partition)

	def get_boot_partition(self) -> Optional[PartitionModification]:
		liltered = filter(lambda x: x.is_boot(), self.partitions)
		return next(liltered, None)

	def get_root_partition(self, relative_path: Optional[Path]) -> Optional[PartitionModification]:
		filtered = filter(lambda x: x.is_root(relative_path), self.partitions)
		return next(filtered, None)

	def __dump__(self) -> Dict[str, Any]:
		"""
		Called when generating configuration files
		"""
		return {
			'device': str(self.device.device_info.path),
			'wipe': self.wipe,
			'partitions': [p.json() for p in self.partitions]
		}


class EncryptionType(Enum):
	NoEncryption = "no_encryption"
	Partition = "partition"

	@classmethod
	def _encryption_type_mapper(cls) -> Dict[str, 'EncryptionType']:
		return {
			# str(_('Full disk encryption')): EncryptionType.FullDiskEncryption,
			str(_('Partition encryption')): EncryptionType.Partition
		}

	@classmethod
	def text_to_type(cls, text: str) -> 'EncryptionType':
		mapping = cls._encryption_type_mapper()
		return mapping[text]

	@classmethod
	def type_to_text(cls, type_: 'EncryptionType') -> str:
		mapping = cls._encryption_type_mapper()
		type_to_text = {type_: text for text, type_ in mapping.items()}
		return type_to_text[type_]


@dataclass
class DiskEncryption:
	encryption_type: EncryptionType = EncryptionType.Partition
	encryption_password: str = ''
	partitions: List[PartitionModification] = field(default_factory=list)
	hsm_device: Optional[Fido2Device] = None

	def should_generate_encryption_file(self, part_mod: PartitionModification) -> bool:
		return part_mod in self.partitions and part_mod.mountpoint != Path('/')

	def json(self) -> Dict[str, Any]:
		obj: Dict[str, Any] = {
			'encryption_type': self.encryption_type.value,
			'partitions': [p.obj_id for p in self.partitions]
		}

		if self.hsm_device:
			obj['hsm_device'] = self.hsm_device.json()

		return obj

	@classmethod
	def parse_arg(
		cls,
		disk_config: DiskLayoutConfiguration,
		arg: Dict[str, Any],
		password: str = ''
	) -> 'DiskEncryption':
		enc_partitions = []
		for mod in disk_config.device_modifications:
			for part in mod.partitions:
				if part.obj_id in arg.get('partitions', []):
					enc_partitions.append(part)

		enc = DiskEncryption(
			EncryptionType(arg['encryption_type']),
			password,
			enc_partitions
		)

		if hsm := arg.get('hsm_device', None):
			enc.hsm_device = Fido2Device.parse_arg(hsm)

		return enc


@dataclass
class Fido2Device:
	path: Path
	manufacturer: str
	product: str

	def json(self) -> Dict[str, str]:
		return {
			'path': str(self.path),
			'manufacturer': self.manufacturer,
			'product': self.product
		}

	@classmethod
	def parse_arg(cls, arg: Dict[str, str]) -> 'Fido2Device':
		return Fido2Device(
			Path(arg['path']),
			arg['manufacturer'],
			arg['product']
		)


@dataclass
class LsblkInfo:
	name: str = ''
	path: Path = Path()
	pkname: str = ''
	size: Size = Size(0, Unit.B)
	log_sec: int = 0
	pttype: str = ''
	ptuuid: str = ''
	rota: bool = False
	tran: Optional[str] = None
	partuuid: Optional[str] = None
	uuid: Optional[str] = None
	fstype: Optional[str] = None
	fsver: Optional[str] = None
	fsavail: Optional[str] = None
	fsuse_percentage: Optional[str] = None
	type: Optional[str] = None
	mountpoint: Optional[Path] = None
	mountpoints: List[Path] = field(default_factory=list)
	fsroots: List[Path] = field(default_factory=list)
	children: List[LsblkInfo] = field(default_factory=list)

	def json(self) -> Dict[str, Any]:
		return {
			'name': self.name,
			'path': str(self.path),
			'pkname': self.pkname,
			'size': self.size.format_size(Unit.MiB),
			'log_sec': self.log_sec,
			'pttype': self.pttype,
			'ptuuid': self.ptuuid,
			'rota': self.rota,
			'tran': self.tran,
			'partuuid': self.partuuid,
			'uuid': self.uuid,
			'fstype': self.fstype,
			'fsver': self.fsver,
			'fsavail': self.fsavail,
			'fsuse_percentage': self.fsuse_percentage,
			'type': self.type,
			'mountpoint': self.mountpoint,
			'mountpoints': [str(m) for m in self.mountpoints],
			'fsroots': [str(r) for r in self.fsroots],
			'children': [c.json() for c in self.children]
		}

	@property
	def btrfs_subvol_info(self) -> Dict[Path, Path]:
		"""
		It is assumed that lsblk will contain the fields as

		"mountpoints": ["/mnt/archinstall/log", "/mnt/archinstall/home", "/mnt/archinstall", ...]
		"fsroots": ["/@log", "/@home", "/@"...]

		we'll thereby map the fsroot, which are the mounted filesystem roots
		to the corresponding mountpoints
		"""
		return dict(zip(self.fsroots, self.mountpoints))

	@classmethod
	def exclude(cls) -> List[str]:
		return ['children']

	@classmethod
	def fields(cls) -> List[str]:
		return [f.name for f in dataclasses.fields(LsblkInfo) if f.name not in cls.exclude()]

	@classmethod
	def from_json(cls, blockdevice: Dict[str, Any]) -> LsblkInfo:
		info = cls()

		for f in cls.fields():
			lsblk_field = _clean_field(f, CleanType.Blockdevice)
			data_field = _clean_field(f, CleanType.Dataclass)

			val: Any = None
			if isinstance(getattr(info, data_field), Path):
				val = Path(blockdevice[lsblk_field])
			elif isinstance(getattr(info, data_field), Size):
				val = Size(blockdevice[lsblk_field], Unit.B)
			else:
				val = blockdevice[lsblk_field]

			setattr(info, data_field, val)

		info.children = [LsblkInfo.from_json(child) for child in blockdevice.get('children', [])]

		# sometimes lsblk returns 'mountpoints': [null]
		info.mountpoints = [Path(mnt) for mnt in info.mountpoints if mnt]

		fs_roots = []
		for r in info.fsroots:
			if r:
				path = Path(r)
				# store the fsroot entries without the leading /
				fs_roots.append(path.relative_to(path.anchor))
		info.fsroots = fs_roots

		return info


class CleanType(Enum):
	Blockdevice = auto()
	Dataclass = auto()
	Lsblk = auto()


def _clean_field(name: str, clean_type: CleanType) -> str:
	match clean_type:
		case CleanType.Blockdevice:
			return name.replace('_percentage', '%').replace('_', '-')
		case CleanType.Dataclass:
			return name.lower().replace('-', '_').replace('%', '_percentage')
		case CleanType.Lsblk:
			return name.replace('_percentage', '%').replace('_', '-')


def _fetch_lsblk_info(dev_path: Optional[Union[Path, str]] = None, retry: int = 3) -> List[LsblkInfo]:
	fields = [_clean_field(f, CleanType.Lsblk) for f in LsblkInfo.fields()]
	lsblk_fields = ','.join(fields)

	if not dev_path:
		dev_path = ''

	if retry == 0:
		retry = 1

	result = None

	for i in range(retry):
		try:
			result = SysCommand(f'lsblk --json -b -o+{lsblk_fields} {dev_path}')
		except SysCallError as error:
			# Get the output minus the message/info from lsblk if it returns a non-zero exit code.
			if error.worker:
				err = error.worker.decode('UTF-8')
				log(f'Error calling lsblk: {err}', level=logging.DEBUG)
				time.sleep(1)
			else:
				raise error

	if result and result.exit_code == 0:
		try:
			if decoded := result.decode('utf-8'):
				block_devices = json.loads(decoded)
				blockdevices = block_devices['blockdevices']
				return [LsblkInfo.from_json(device) for device in blockdevices]
		except json.decoder.JSONDecodeError as err:
			log(f"Could not decode lsblk JSON: {result}", fg="red", level=logging.ERROR)
			raise err

	raise DiskError(f'Failed to read disk "{dev_path}" with lsblk')


def get_lsblk_info(dev_path: Union[Path, str]) -> LsblkInfo:
	if infos := _fetch_lsblk_info(dev_path):
		return infos[0]

	raise DiskError(f'lsblk failed to retrieve information for "{dev_path}"')


def get_all_lsblk_info() -> List[LsblkInfo]:
	return _fetch_lsblk_info()


def get_lsblk_by_mountpoint(mountpoint: Path, as_prefix: bool = False) -> List[LsblkInfo]:
	def _check(infos: List[LsblkInfo]) -> List[LsblkInfo]:
		devices = []
		for entry in infos:
			if as_prefix:
				matches = [m for m in entry.mountpoints if str(m).startswith(str(mountpoint))]
				if matches:
					devices += [entry]
			elif mountpoint in entry.mountpoints:
				devices += [entry]

			if len(entry.children) > 0:
				if len(match := _check(entry.children)) > 0:
					devices += match

		return devices

	all_info = get_all_lsblk_info()
	return _check(all_info)
