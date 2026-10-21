package:
	$(call py_action,package_appservices_ios,$(ABS_DIST)/appservices-ios)

upload:
	cp $(ABS_DIST)/appservices-ios/*.zip $(UPLOAD_PATH)/
