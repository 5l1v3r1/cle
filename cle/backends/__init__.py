import os
import subprocess


import archinfo
from ..address_translator import AT
from ..memory import Clemory
from ..errors import CLEOperationError, CLEError
from ..utils import key_bisect_find, key_bisect_insort_left

try:
    import claripy
except ImportError:
    claripy = None

import logging
l = logging.getLogger('cle.backends')


class Region(object):
    """
    A region of memory that is mapped in the object's file.

    :ivar offset:       The offset into the file the region starts.
    :ivar vaddr:        The virtual address.
    :ivar filesize:     The size of the region in the file.
    :ivar memsize:      The size of the region when loaded into memory.

    The prefix `v-` on a variable or parameter name indicates that it refers to the virtual, loaded memory space,
    while a corresponding variable without the `v-` refers to the flat zero-based memory of the file.

    When used next to each other, `addr` and `offset` refer to virtual memory address and file offset, respectively.
    """
    def __init__(self, offset, vaddr, filesize, memsize):
        self.vaddr = vaddr
        self.memsize = memsize
        self.filesize = filesize
        self.offset = offset

    def _rebase(self, delta):
        """
        Does region rebasing to other base address.
        Intended for usage by loader's add_object to reflect the rebasing.

        :param delta: Delta offset between an old and a new image bases
        :type delta: int
        """
        self.vaddr += delta

    def contains_addr(self, addr):
        """
        Does this region contain this virtual address?
        """
        return self.vaddr <= addr < self.vaddr + self.memsize

    def contains_offset(self, offset):
        """
        Does this region contain this offset into the file?
        """
        return self.offset <= offset < self.offset + self.filesize

    def addr_to_offset(self, addr):
        """
        Convert a virtual memory address into a file offset
        """
        offset = addr - self.vaddr + self.offset
        if not self.contains_offset(offset):
            return None
        return offset

    def offset_to_addr(self, offset):
        """
        Convert a file offset into a virtual memory address
        """
        addr = offset - self.offset + self.vaddr
        if not self.contains_addr(addr):
            return None
        return addr

    def __repr__(self):
        return '{}({})'.format(self.__class__, ', '.join(['{}=0x{:x}'.format(k, v) for k, v in self.__dict__.iteritems()]))

    @property
    def max_addr(self):
        """
        The maximum virtual address of this region
        """
        return self.vaddr + self.memsize - 1

    @property
    def min_addr(self):
        """
        The minimum virtual address of this region
        """
        return self.vaddr

    @property
    def max_offset(self):
        """
        The maximum file offset of this region
        """
        return self.offset + self.filesize - 1

    def min_offset(self):
        """
        The minimum file offset of this region
        """
        return self.offset


class Segment(Region):
    """
    Simple representation of an ELF file segment.
    """
    pass


class Section(Region):
    """
    Simple representation of a loaded section.

    :ivar str name:     The name of the section
    """
    def __init__(self, name, offset, vaddr, size):
        """
        :param str name:    The name of the section
        :param int offset:  The offset into the binary file this section begins
        :param int vaddr:   The address in virtual memory this section begins
        :param int size:    How large this section is
        """
        super(Section, self).__init__(offset, vaddr, size, size)
        self.name = name

    @property
    def is_readable(self):
        """
        Whether this section has read permissions
        """
        raise NotImplementedError()

    @property
    def is_writable(self):
        """
        Whether this section has write permissions
        """
        raise NotImplementedError()

    @property
    def is_executable(self):
        """
        Whether this section has execute permissions
        """
        raise NotImplementedError()

    def __repr__(self):
        return "<%s | offset %#x, vaddr %#x, size %#x>" % (
            self.name if self.name else "Unnamed",
            self.offset,
            self.vaddr,
            self.memsize
        )


class Symbol(object):
    """
    Representation of a symbol from a binary file. Smart enough to rebase itself.

    There should never be more than one Symbol instance representing a single symbol. To make sure of this, only use
    the :meth:`cle.backends.Backend.get_symbol()` to create new symbols.

    :ivar owner_obj:        The object that contains this symbol
    :vartype owner_obj:     cle.backends.Backend
    :ivar str name:         The name of this symbol
    :ivar int addr:         The un-based address of this symbol, an RVA
    :iver int size:         The size of this symbol
    :ivar int type:         The type of this symbol as one of SYMBOL.TYPE_*
    :ivar bool resolved:    Whether this import symbol has been resolved to a real symbol
    :ivar resolvedby:       The real symbol this import symbol has been resolve to
    :vartype resolvedby:    None or cle.backends.Symbol
    :ivar str resolvewith:  The name of the library we must use to resolve this symbol, or None if none is required.
    """

    # enum for symbol types
    TYPE_OTHER = 0
    TYPE_NONE = 1
    TYPE_FUNCTION = 2
    TYPE_OBJECT = 3
    TYPE_SECTION = 4

    def __init__(self, owner, name, addr, size, sym_type):
        """
        Not documenting this since if you try calling it, you're wrong.
        """
        super(Symbol, self).__init__()
        self.owner_obj = owner
        self.name = name
        self.addr = addr
        self.size = size
        self.type = sym_type
        self.resolved = False
        self.resolvedby = None
        if (claripy and isinstance(self.addr, claripy.ast.Base)) or self.addr != 0:
            self.owner_obj._symbols_by_addr[self.addr] = self
            # would be nice if we could populate demangled_names here...

            #demangled = self.demangled_name
            #if demangled is not None:
            #    self.owner_obj.demangled_names[self.name] = demangled

    def resolve(self, obj):
        self.resolved = True
        self.resolvedby = obj
        self.owner_obj.resolved_imports.append(self)

    @property
    def rebased_addr(self):
        """
        The address of this symbol in the global memory space
        """
        return AT.from_rva(self.addr, self.owner_obj).to_mva()

    @property
    def is_function(self):
        """
        Whether this symbol is a function
        """
        return self.type == Symbol.TYPE_FUNCTION

    # These may be overridden in subclasses
    is_static = False
    is_common = False
    is_import = False
    is_export = False
    is_weak = False
    is_extern = False

    @property
    def demangled_name(self):
        """
        The name of this symbol, run through a C++ demangler

        Warning: this calls out to the external program `c++filt` and will fail loudly if it's not installed
        """
        # make sure it's mangled
        if self.name.startswith("_Z"):
            name = self.name
            if '@@' in self.name:
                name = self.name.split("@@")[0]
            args = ['c++filt']
            args.append(name)
            pipe = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            stdout, _ = pipe.communicate()
            demangled = stdout.split("\n")

            if demangled:
                return demangled[0]

        return None


#
# Container
#


class Regions(object):
    """
    A container class acting as a list of regions (sections or segments). Additionally, it keeps an sorted list of
    those regions to allow fast lookups.

    We assume none of the regions overlap with others.
    """

    def __init__(self, lst=None):
        self._list = lst if lst is not None else []

        if self._list:
            self._sorted_list = self._make_sorted(self._list)
        else:
            self._sorted_list = []

    @property
    def raw_list(self):
        """
        Get the internal list. Any change to it is not tracked, and therefore _sorted_list will not be updated.
        Therefore you probably does not want to modify the list.

        :return:  The internal list container.
        :rtype:   list
        """

        return self._list

    @property
    def max_addr(self):
        """
        Get the highest address of all regions.

        :return: The highest address of all regions, or None if there is no region available.
        rtype:   int or None
        """

        if self._sorted_list:
            return self._sorted_list[-1].max_addr
        return None

    def __getitem__(self, idx):
        return self._list[idx]

    def __setitem__(self, idx, item):
        self._list[idx] = item

        # update self._sorted_list
        self._sorted_list = self._make_sorted(self._list)

    def __len__(self):
        return len(self._list)

    def __repr__(self):
        return "<Regions: %s>" % repr(self._list)

    def _rebase(self, delta):
        """
        Does regions rebasing to other base address.
        Modifies state of each internal object, so the list reference doesn't need to be updated,
        the same is also valid for sorted list as operation preserves the ordering.

        :param delta: Delta offset between an old and a new image bases
        :type delta: int
        """
        map(lambda x: x._rebase(delta), self._list)

    def append(self, region):
        """
        Append a new Region instance into the list.

        :param Region region: The region to append.
        :return: None
        """

        self._list.append(region)
        key_bisect_insort_left(self._sorted_list, region, keyfunc=lambda r: r.vaddr)

    def find_region_containing(self, addr):
        """
        Find the region that contains a specific address. Returns None if none of the regions covers the address.

        :param addr:    The address.
        :type addr:     int
        :return:        The region that covers the specific address, or None if no such region is found.
        :rtype:         Region or None
        """

        pos = key_bisect_find(self._sorted_list, addr,
                              keyfunc=lambda r: r if type(r) in (int, long) else r.vaddr + r.memsize)
        if pos >= len(self._sorted_list):
            return None
        region = self._sorted_list[pos]
        if region.contains_addr(addr):
            return region
        return None

    @staticmethod
    def _make_sorted(lst):
        """
        Return a sorted list of regions.

        :param list lst:  A list of regions.
        :return:          A sorted list of regions.
        :rtype:           list
        """

        return sorted(lst, key=lambda x: x.vaddr)


class Backend(object):
    """
    Main base class for CLE binary objects.

    An alternate interface to this constructor exists as the static method :meth:`cle.loader.Loader.load_object`

    :ivar binary:           The path to the file this object is loaded from
    :ivar is_main_bin:      Whether this binary is loaded as the main executable
    :ivar segments:         A listing of all the loaded segments in this file
    :ivar sections:         A listing of all the demarked sections in the file
    :ivar sections_map:     A dict mapping from section name to section
    :ivar imports:          A mapping from symbol name to import symbol
    :ivar resolved_imports: A list of all the import symbols that are successfully resolved
    :ivar relocs:           A list of all the relocations in this binary
    :ivar irelatives:       A list of tuples representing all the irelative relocations that need to be performed. The
                            first item in the tuple is the address of the resolver function, and the second item is the
                            address of where to write the result. The destination address is not rebased.
    :ivar jmprel:           A mapping from symbol name to the address of its jump slot relocation, i.e. its GOT entry.
    :ivar arch:             The architecture of this binary
    :vartype arch:          archinfo.arch.Arch
    :ivar str os:           The operating system this binary is meant to run under
    :ivar int mapped_base:  The base address of this object in virtual memory
    :ivar deps:             A list of names of shared libraries this binary depends on
    :ivar linking:          'dynamic' or 'static'
    :ivar linked_base:      The base address this object requests to be loaded at
    :ivar bool pic:         Whether this object is position-independent
    :ivar bool execstack:   Whether this executable has an executable stack
    :ivar str provides:     The name of the shared library dependancy that this object resolves
    """

    def __init__(self,
            binary,
            loader=None,
            is_main_bin=False,
            filename=None,
            custom_entry_point=None,
            custom_arch=None,
            custom_base_addr=None,
            **kwargs):
        """
        :param binary:          The path to the binary to load
        :param is_main_bin:     Whether this binary should be loaded as the main executable
        """
        if hasattr(binary, 'seek') and hasattr(binary, 'read'):
            self.binary = filename
            self.binary_stream = binary
        else:
            self.binary = binary
            try:
                self.binary_stream = open(binary, 'rb')
            except IOError:
                self.binary_stream = None

        if kwargs != {}:
            l.warning("Unused kwargs for loading binary %s: %s", self.binary, ', '.join(kwargs.iterkeys()))

        self.is_main_bin = is_main_bin
        self.loader = loader
        self._entry = None
        self._segments = Regions() # List of segments
        self._sections = Regions() # List of sections
        self.sections_map = {}  # Mapping from section name to section
        self._symbols_by_addr = {}
        self.imports = {}
        self.resolved_imports = []
        self.relocs = []
        self.irelatives = []    # list of tuples (resolver, destination), dest w/o rebase
        self.jmprel = {}
        self.arch = None
        self.os = None  # Let other stuff override this
        self._symbol_cache = {}

        self.mapped_base_symbolic = 0
        # These are set by cle, and should not be overriden manually
        self.mapped_base = self.linked_base = 0 # not to be set manually - used by CLE

        self.deps = []           # Needed shared objects (libraries dependencies)
        self.linking = None # Dynamic or static linking
        self.pic = False
        self.execstack = False

        # Custom options
        self._custom_entry_point = custom_entry_point
        self._custom_base_addr = custom_base_addr
        self.provides = os.path.basename(self.binary) if self.binary is not None else None

        self.memory = None

        # should be set inside `cle.Loader.add_object`
        self._is_mapped = False

        if custom_arch is None:
            self.arch = None
        elif isinstance(custom_arch, str):
            self.set_arch(archinfo.arch_from_id(custom_arch))
        elif isinstance(custom_arch, archinfo.Arch):
            self.set_arch(custom_arch)
        elif isinstance(custom_arch, type) and issubclass(custom_arch, archinfo.Arch):
            self.set_arch(custom_arch())
        else:
            raise CLEError("Bad parameter: custom_arch=%s" % custom_arch)

    def close(self):
        if self.binary_stream is not None:
            self.binary_stream.close()
            self.binary_stream = None

    def __repr__(self):
        if self.binary is not None:
            return '<%s Object %s, maps [%#x:%#x]>' % \
                   (self.__class__.__name__, os.path.basename(self.binary), self.min_addr, self.max_addr)
        else:
            return '<%s Object from stream, maps [%#x:%#x]>' % \
                   (self.__class__.__name__, self.min_addr, self.max_addr)

    def set_arch(self, arch):
        self.arch = arch
        self.memory = Clemory(arch) # Private virtual address space, without relocations

    @property
    def image_base_delta(self):
        return self.mapped_base - self.linked_base

    @property
    def entry(self):
        if self._custom_entry_point is not None:
            return AT.from_lva(self._custom_entry_point, self).to_mva()
        return AT.from_lva(self._entry, self).to_mva()

    @property
    def segments(self):
        return self._segments

    @segments.setter
    def segments(self, v):
        if isinstance(v, list):
            self._segments = Regions(lst=v)
        elif isinstance(v, Regions):
            self._segments = v
        else:
            raise ValueError('Unsupported type %s set as sections.' % type(v))

    @property
    def sections(self):
        return self._sections

    @sections.setter
    def sections(self, v):
        if isinstance(v, list):
            self._sections = Regions(lst=v)
        elif isinstance(v, Regions):
            self._sections = v
        else:
            raise ValueError('Unsupported type %s set as sections.' % type(v))

    @property
    def symbols_by_addr(self):
        return {AT.from_rva(x, self).to_mva(): self._symbols_by_addr[x] for x in self._symbols_by_addr}

    def rebase(self):
        """
        Rebase backend's regions to the new base where they were mapped by the loader
        """
        if self._is_mapped:
            raise CLEOperationError("Image already rebased from %#x to %#x" % (self.linked_base, self.mapped_base))
        if self.sections:
            self.sections._rebase(self.image_base_delta)
        if self.segments:
            self.segments._rebase(self.image_base_delta)

    def contains_addr(self, addr):
        """
        Is `addr` in one of the binary's segments/sections we have loaded? (i.e. is it mapped into memory ?)
        """
        return self.find_loadable_containing(addr) is not None

    def find_loadable_containing(self, addr):
        lookup = self.find_segment_containing if self.segments else self.find_section_containing
        return lookup(addr)

    def find_segment_containing(self, addr):
        """
        Returns the segment that contains `addr`, or ``None``.
        """
        return self.segments.find_region_containing(addr)

    def find_section_containing(self, addr):
        """
        Returns the section that contains `addr` or ``None``.
        """
        return self.sections.find_region_containing(addr)

    def addr_to_offset(self, addr):
        loadable = self.find_loadable_containing(addr)
        if loadable is not None:
            return loadable.addr_to_offset(addr)
        else:
            return None

    def offset_to_addr(self, offset):
        if self.segments:
            for s in self.segments:
                if s.contains_offset(offset):
                    return s.offset_to_addr(offset)
        else:
            for s in self.sections:
                if s.contains_offset(offset):
                    return s.offset_to_addr(offset)

    @property
    def min_addr(self):
        """
        This returns the lowest virtual address contained in any loaded segment of the binary.
        """
        # Loader maps the object at chosen mapped base anyway and independently of the internal structure
        return self.mapped_base

    @property
    def max_addr(self):
        """
        This returns the highest virtual address contained in any loaded segment of the binary.
        """

        # TODO: The access should be constant time, as the region interval is immutable after load
        out = self.mapped_base
        if self.segments or self.sections:
            out = max(map(lambda x: x.max_addr, self.segments or self.sections))
        return out

    @property
    def initializers(self): # pylint: disable=no-self-use
        """
        Stub function. Should be overridden by backends that can provide initializer functions that ought to be run
        before execution reaches the entry point. Addresses should be rebased.
        """
        return []

    @property
    def finalizers(self): # pylint: disable=no-self-use
        """
        Stub function. Like initializers, but with finalizers.
        """
        return []

    def get_symbol(self, name): # pylint: disable=no-self-use,unused-argument
        """
        Stub function. Implement to find the symbol with name `name`.
        """
        if name in self._symbol_cache:
            return self._symbol_cache[name]
        return None

    @staticmethod
    def extract_soname(path):
        """
        Extracts the shared object identifier from the path, or returns None if it cannot.
        """
        return None

    @classmethod
    def check_compatibility(cls, spec, other_obj): # pylint: disable=unused-argument
        """
        Performs a minimal static load of ``spec`` and returns whether it's compatible with other_obj
        """
        return False

ALL_BACKENDS = dict()


def register_backend(name, cls):
    if not hasattr(cls, 'is_compatible'):
        raise TypeError("Backend needs an is_compatible() method")
    ALL_BACKENDS.update({name: cls})


from .elf import ELF
from .elfcore import ELFCore
from .pe import PE
from .idabin import IDABin
from .blob import Blob
from .cgc import CGC
from .backedcgc import BackedCGC
from .metaelf import MetaELF
from .hex import Hex
