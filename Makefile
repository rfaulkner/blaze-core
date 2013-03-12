.PHONY: all docs tests build clean web

CC = gcc
LPYTHON = $(shell python-config --includes --libs)
CFLAGS = -shared -fPIC -lpthread $(LPYTHON)

all: build

# stupid hack for now
blir:
	$(CC) $(CFLAGS) blaze/blir/datashape.c -o blaze/blir/datashape.o
	$(CC) $(CFLAGS) blaze/blir/prelude.c blaze/blir/datashape.o -o blaze/blir/prelude.dylib

build:
	python setup.py build_ext --inplace

tests:
	nosetests -s -v --detailed blaze

docs:
	cd docs; make html

images:
	cd docs/source/svg; make

web:
	cd web; make html

cleandocs:
	cd docs; make clean

clean:
	python setup.py clean
