from pathlib import Path

from setuptools import find_packages, setup


setup(
    name="podvault",
    version="0.1.1",
    description="Safely save and restore ephemeral GPU-pod projects with Kopia",
    long_description=(Path(__file__).parent / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    license="Apache-2.0",
    url="https://github.com/konpuringu/podvault",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages("src"),
    entry_points={"console_scripts": ["podvault=podvault.cli:main"]},
    include_package_data=True,
)
