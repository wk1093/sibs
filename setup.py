from setuptools import setup, find_packages
from sibs._version import sibsversion

setup(
    name='sibs-build',
    version=sibsversion,
    description='A simple integrated build system',
    url='https://github.com/wk1093/sibs',
    author='wk1093',
    author_email='wyattk1093@gmail.com',
    license='GNU General Public License v3.0',
    packages=['sibs'],
    install_requires=[
        
    ],
    entry_points={
        'console_scripts': [
            'sibs = sibs.__main__:main',
        ],
    },
)