#!/usr/bin/env python

# @jerinphilip
# Probably a bad idea, but I need a way to import this as a module in a fork.

from setuptools import setup, find_packages

with open('requirements.txt') as f:
    required = f.read().splitlines()


setup(name='musicbot',
      version='1.0',
      description='MusicBot as a module',
      author='Anonymous',
      author_email='user@example.com',
      url='https://github.com/jerinphilip/MusicBot',
      packages=find_packages(),
      install_requires=required,
)


