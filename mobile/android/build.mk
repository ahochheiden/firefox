# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

include  $(topsrcdir)/toolkit/mozapps/installer/package-name.mk

ifdef MOZ_USE_LEGACY_PACKAGING
package:
	@$(MAKE) -C mobile/android/installer

stage-package:
	$(MAKE) -C mobile/android/installer stage-package

upload::
	@$(MAKE) -C mobile/android/installer upload
else
package:
	$(call py_action,package_configured,--mode=package --ab-cd=$(AB_CD))

stage-package:
	$(call py_action,package_configured,--mode=stage --ab-cd=$(AB_CD))

upload::
	$(call py_action,package_upload,--ab-cd=$(AB_CD) $(if $(MOZ_SIMPLE_PACKAGE_NAME),--simple-package-name=$(MOZ_SIMPLE_PACKAGE_NAME)) $(addprefix --extra-file=,$(UPLOAD_EXTRA_FILES)))
endif

wget-en-US:
	@$(MAKE) -C mobile/android/locales $@

# make -j1 because dependencies in l10n build targets don't work
# with parallel builds
merge-% chrome-%:
	$(MAKE) -j1 -C mobile/android/locales $@
