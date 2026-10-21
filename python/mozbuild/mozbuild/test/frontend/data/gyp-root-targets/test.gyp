{
  'target_defaults': {
    'configurations': {
      'Release': {},
    },
  },
  'targets': [
    {
      'target_name': 'a',
      'type': 'static_library',
      'dependencies': ['other.gyp:b'],
    },
    {
      'target_name': 'c',
      'type': 'static_library',
    },
  ],
}
