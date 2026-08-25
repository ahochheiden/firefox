ifdef MOZ_USE_LEGACY_PACKAGING
package:
	$(MAKE) -C mobile/ios/installer

upload:
	$(MAKE) -C mobile/ios/installer upload
else
package:
	$(call py_action,package_configured,--mode=package --ab-cd=$(AB_CD))

upload:
	$(call py_action,package_upload,--ab-cd=$(AB_CD) $(if $(MOZ_SIMPLE_PACKAGE_NAME),--simple-package-name=$(MOZ_SIMPLE_PACKAGE_NAME)) $(addprefix --extra-file=,$(UPLOAD_EXTRA_FILES)))
endif
