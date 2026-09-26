# A rewriter that fails mid-write leaves a TRUNCATED file that make then treats as finished,
# because make does not delete the target of a failed recipe by default. The rewriters exit
# non-zero on a pattern mismatch by design, which is exactly when this bites. `>` has already
# created the file at that point. This makes make clean it up instead.
.DELETE_ON_ERROR:

PROGRAM := AltServer

ARCH := $(shell gcc -dumpmachine | cut -d- -f 1)

PROGRAM := $(PROGRAM)-$(ARCH)

CFLAGS := -DDEBUG -O0 -g

ifeq ($(ARCH),i386)
CFLAGS += -mno-default
endif

ifeq ($(ARCH),i686)
CFLAGS += -mno-default
endif

CXXFLAGS = $(CFLAGS) -std=c++17

CFLAGS += -DNO_USBMUXD_STUB

ROOT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
include $(ROOT_DIR)/makefiles/main.mak

# ONE recipe, not two. This used to be a multi-target rule naming both archives, which GNU make
# expands into two independent targets each carrying the same recursive recipe. Both are .PHONY so
# both always ran, and libplist.a is reachable through lib_AltSign while libimobiledevice.a is
# reachable through lib_libimobiledevice -- so under -j, which every shipping build uses, the
# sub-make ran TWICE CONCURRENTLY. That meant every object in it compiled twice to the same path
# and `ar rcs` ran twice on the same archive: wasted time, and two cc processes writing one .o is
# a corruption risk that would surface as an unrelated link error.
#
# libplist.a needs no recipe of its own: the sub-make's `all` target builds both archives, so
# depending on libimobiledevice.a both orders it correctly and produces it.
$(BUILD_DIR)/libimobiledevice.a :
	$(MAKE) -f $(ROOT_DIR)/makefiles/libimobiledevice-build/libimobiledevice.mak
$(BUILD_DIR)/libplist.a : $(BUILD_DIR)/libimobiledevice.a
lib_libimobiledevice: $(BUILD_DIR)/libimobiledevice.a $(BUILD_DIR)/libplist.a
lib_libimobiledevice_clean : 
	$(MAKE) -f $(ROOT_DIR)/makefiles/libimobiledevice-build/libimobiledevice.mak clean
.PHONY: $(BUILD_DIR)/libimobiledevice.a $(BUILD_DIR)/libplist.a lib_libimobiledevice lib_libimobiledevice_clean

$(BUILD_DIR)/AltSign.a:
	$(MAKE) -f $(ROOT_DIR)/makefiles/AltSign-build/AltSign.mak EXTRA_FLAGS=$(libplist_include)
lib_AltSign : $(BUILD_DIR)/AltSign.a $(BUILD_DIR)/libplist.a
lib_AltSign_clean : 
	$(MAKE) -f $(ROOT_DIR)/makefiles/AltSign-build/AltSign.mak clean
.PHONY: $(BUILD_DIR)/AltSign.a $(BUILD_DIR)/libplist.a lib_AltSign lib_AltSign_clean

$(BUILD_DIR)/dnssd_loader.a:
	$(MAKE) -f $(ROOT_DIR)/makefiles/dnssd_loader-build/dnssd_loader.mak
lib_dnssd_loader : $(BUILD_DIR)/dnssd_loader.a
lib_dnssd_loader_clean : 
	$(MAKE) -f $(ROOT_DIR)/makefiles/dnssd_loader-build/dnssd_loader.mak clean
.PHONY: $(BUILD_DIR)/dnssd_loader.a lib_dnssd_loader lib_dnssd_loader_clean

# idevice's C API for src/DevicePairingManager.cpp. See makefiles/idevice-build/idevice.mak for
# why it is one prelinked object rather than the static library cargo produces.
$(BUILD_DIR)/idevice_ffi.o:
	$(MAKE) -f $(ROOT_DIR)/makefiles/idevice-build/idevice.mak
lib_idevice : $(BUILD_DIR)/idevice_ffi.o
lib_idevice_clean :
	$(MAKE) -f $(ROOT_DIR)/makefiles/idevice-build/idevice.mak clean
.PHONY: $(BUILD_DIR)/idevice_ffi.o lib_idevice lib_idevice_clean


include $(ROOT_DIR)/makefiles/libimobiledevice-build/libimobiledevice-files.mak
include $(ROOT_DIR)/makefiles/AltSign-build/AltSign-files.mak
include $(ROOT_DIR)/makefiles/dnssd_loader-build/dnssd_loader-files.mak
include $(ROOT_DIR)/makefiles/idevice-build/idevice-files.mak

#libimobiledevice_include := -I$(LIB_DIR)/libimobiledevice/include -I$(LIB_DIR)/libimobiledevice -I$(LIB_DIR)/libusbmuxd/include
#libplist_include := -I$(LIB_DIR)/libplist/include
#altsign_include := -I$(BUILD_DIR)/AltSign_patched

#INC_CFLAGS := -Ilibraries
INC_CFLAGS += $(libimobiledevice_include)
INC_CFLAGS += $(libplist_include)
INC_CFLAGS += $(altsign_include)
INC_CFLAGS += $(dnssd_loader_include)
INC_CFLAGS += $(idevice_include)

include $(ROOT_DIR)/makefiles/AltWindowsShim.mak

main_srcroot := $(UPSTREAM_DIR)/AltServer
main_override_srcroot := $(ROOT_DIR)/src
main_override_src := $(wildcard $(main_override_srcroot)/*.cpp)

main_orisrc := $(wildcard $(main_srcroot)/*.cpp) $(wildcard $(main_srcroot)/*.c)

main_orisrc := $(filter-out $(main_srcroot)/AltServer.cpp, $(main_orisrc))
main_orisrc := $(filter-out $(main_override_src:$(main_override_srcroot)/%=$(main_srcroot)/%), $(main_orisrc))
#main_orisrc := $(filter-out $(main_srcroot)/AltServerApp.cpp, $(main_orisrc))
#main_orisrc := $(filter-out $(main_srcroot)/AnisetteDataManager.cpp, $(main_orisrc))

#$(BUILD_DIR)/objs/%.c.o : $(BUILD_DIR)/%.c
#	python3 $(ROOT_DIR)/makefiles/AltSign-build/rewrite_altserver_source.py "$<" | $(CC) -x c -I`dirname $<` $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c -
#	#$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<
#
#$(BUILD_DIR)/objs/%.cpp.o : $(BUILD_DIR)/%.cpp
#	python3 $(ROOT_DIR)/makefiles/AltSign-build/rewrite_altserver_source.py "$<" | $(CXX) -x c++ -I`dirname $<` $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c -
#	#$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

main_patched_root := $(BUILD_DIR)/AltServer_patched
main_orifiles := $(wildcard $(main_srcroot)/*.*)
main_newfiles := $(main_orifiles:$(main_srcroot)/%=$(main_patched_root)/%)

# The rewriter is a PREREQUISITE, not just something the recipe happens to run. Without it,
# editing a rewriter regenerated NOTHING: make compared only the vendored source against the
# patched output, found it newer, and reported a completely successful build with the previous
# patch compiled in. `make clean` did not help either, because it never removed the patched tree.
# Since this project's entire patching strategy is "patches to vendored code live in the
# rewriters", the documented way to change vendored behaviour silently did nothing.
$(main_patched_root)/%: $(main_srcroot)/% $(ROOT_DIR)/makefiles/rewrite_altserver_source.py
	mkdir -p `dirname "$@"`
	python3 $(ROOT_DIR)/makefiles/rewrite_altserver_source.py "$<" > $@

$(main_newfiles) : $(main_orifiles)

preprocess : $(main_newfiles)
.PHONY : preprocess


$(BUILD_DIR)/objs/%.c.o : $(BUILD_DIR)/%.c
	mkdir -p $(@D)
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

$(BUILD_DIR)/objs/%.cpp.o : $(BUILD_DIR)/%.cpp
	mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

$(BUILD_DIR)/objs/%.c.o : $(ROOT_DIR)/%.c
	mkdir -p $(@D)
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

$(BUILD_DIR)/objs/%.cpp.o : $(ROOT_DIR)/%.cpp
	mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

main_newsrc := $(main_orisrc:$(main_srcroot)/%=$(main_patched_root)/%)
#main_objs = $(main_orisrc:$(UPSTREAM_DIR)/%=$(BUILD_DIR)/objs/%.o)

main_objs = $(main_newsrc:$(BUILD_DIR)/%=$(BUILD_DIR)/objs/%.o) $(main_override_src:$(ROOT_DIR)/%=$(BUILD_DIR)/objs/%.o) $(shim_src:$(MAIN_DIR)/%=$(BUILD_DIR)/objs/%.o)

$(main_objs) : lib_AltSign lib_libimobiledevice lib_dnssd_loader lib_idevice

$(main_objs) : EXTRA_FLAGS := -I$(main_patched_root) -I$(ROOT_DIR)/src -fpermissive -include "common.h" $(INC_CFLAGS)

LDFLAGS = -static -lssl -lcrypto -lpthread -lcorecrypto_static -lzip -lm -lz -lcpprest -lboost_system -lboost_filesystem -lstdc++ -lssl -lcrypto -luuid

$(BUILD_DIR)/$(PROGRAM):: $(main_objs) $(BUILD_DIR)/libimobiledevice.a $(BUILD_DIR)/AltSign.a $(BUILD_DIR)/libplist.a $(BUILD_DIR)/dnssd_loader.a $(BUILD_DIR)/idevice_ffi.o
	$(CC) -o $@ $^ $(LDFLAGS)

.PHONY: clean all
clean:: lib_libimobiledevice_clean lib_AltSign_clean lib_dnssd_loader_clean lib_idevice_clean
	rm -f $(main_objs) $(BUILD_DIR)/$(PROGRAM)
	# The PATCHED tree, not just the objects. Leaving it behind meant `make clean && make`
	# rebuilt every object from the stale rewritten sources, so a clean build was not clean.
	rm -rf $(main_patched_root)

all:: preprocess $(BUILD_DIR)/$(PROGRAM)
.DEFAULT_GOAL := all
