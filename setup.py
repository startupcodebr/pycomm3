# -*- coding: utf-8 -*-
from setuptools import setup, find_packages
from pycomm3 import __version__
import os


def read(file_name):
    return open(os.path.join(os.path.dirname(__file__), file_name)).read()


setup(
    name="pycomm3",
    version=__version__,
    author='Startup Code',
    author_email="suporte@startupcode.com.br",
    url="https://github.com/startupcodebr/pycomm3",
    description="A PLC communication library for Python",
    long_description=read('README.rst'),
    license="MIT",
    packages=find_packages(),
    python_requires='>=3.6',
    install_requires=['autologging', 'pywin32;platform_system=="Windows"'],
    include_package_data=True,
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Intended Audience :: Manufacturing',
        'Natural Language :: English',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: Scientific/Engineering :: Interface Engine/Protocol Translator',
        'Topic :: Scientific/Engineering :: Human Machine Interfaces',
    ],
)
