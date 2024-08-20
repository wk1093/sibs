# Maintainer: wk1093 <wyattk1093@gmail.com>


makedepends=(python-build python-installer python-wheel)

pkgname=python-sibs
pkgver=0.1.2
pkgrel=1
pkgdesc="Simple Integrated Build System for C/C++"
arch=(any)
url="https://github.com/wk1093/sibs"
license=(GPL-3.0-or-later)
_name=${pkgname#python-}

source=("$pkgname-$pkgver.tar.gz::https://github.com/wk1093/sibs/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd $_name-$pkgver
    python -m build --wheel --no-isolation
}

package() {
    cd $_name-$pkgver

    python -m installer --destdir="$pkgdir" dist/*.whl
}