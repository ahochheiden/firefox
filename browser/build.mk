# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

ifdef MOZ_USE_LEGACY_PACKAGING
package:
	@$(MAKE) -C browser/installer

stage-package:
	@$(MAKE) -C browser/installer stage-package

install::
	@$(MAKE) -C browser/installer install

upload::
	@$(MAKE) -C browser/installer upload
else
package:
	$(call py_action,package_configured,--mode=package --ab-cd=$(AB_CD))

stage-package:
	$(call py_action,package_configured,--mode=stage --ab-cd=$(AB_CD))

install::
	$(call py_action,package_configured,--mode=install --ab-cd=$(AB_CD))

upload::
	$(call py_action,package_upload,--ab-cd=$(AB_CD) $(if $(MOZ_SIMPLE_PACKAGE_NAME),--simple-package-name=$(MOZ_SIMPLE_PACKAGE_NAME)) $(addprefix --extra-file=,$(UPLOAD_EXTRA_FILES)))
endif

package-compare:
	@$(MAKE) -C browser/installer package-compare

wget-en-US:
	@$(MAKE) -C browser/locales $@

ifdef MAKENSISU
INSTALLER_REPACK_DEPS = browser/installer/windows/nsis-stage.stamp
endif

installers-%: $(INSTALLER_REPACK_DEPS)
	$(MAKE) -C browser/locales $@

merge-% langpack-% chrome-%:
	$(MAKE) -C browser/locales $@
