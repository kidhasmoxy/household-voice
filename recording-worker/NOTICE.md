# Third-party software

The MIT license in this directory covers Household Voice recording-worker code.
The container retains third-party software and its own license metadata.

The tested candidate uses the official Python 3.14 Alpine base image. Python and
Alpine packages retain their upstream licenses. Installed Python distribution
metadata and license files are preserved in the image.

The [build receipt](tests/evidence/image-build.json) records exact tested versions
and their installed license metadata. These distributions report:

| Distribution | Reported license |
| --- | --- |
| aiohttp | Apache-2.0 AND MIT |
| aiohappyeyeballs | PSF-2.0 |
| aiosignal, frozenlist, multidict, propcache, yarl | Apache-2.0 |
| attrs, pip | MIT |
| idna | BSD-3-Clause |

The integration's recording validator is supplied only to test fixtures; the
worker image contains no Home Assistant integration implementation and does not
duplicate the integration's native question/answer validator.
