# What file format/database format does Picasa use?

> Archived copy for reverse-engineering reference. Source: <https://superuser.com/questions/151146/what-file-format-database-format-does-picasa-use>
> Retrieved 2026-06-11 (text extracted with trafilatura). Stack Exchange content is CC BY-SA.

---

`.PMP` is a format proprietory to Picasa, used to store informations about images. 
( Reference )

( Note: Below referenced blog post is dated, not sure how relevant/correct it is to the current version of Picasa )

More info:

  in the db2 directory there are a
  number of files. The important files
  for this are `albumdata_token.pmp`,
  `albumdata_uid.pmp` and
  `albumdata_name.pmp`

  
  Here are the contents of the files:

  
  `albumdata_name.pmp` - 

  
  this is the name of the albums in
  picasa. The first two
  are defaults and are not included in
  any of the other files. 


```
Starred Photos
Screensaver 
root
modified_for_tags
sam3 
Sample Pictures 
Sammy
```

  `albumdata_uid.pmp` - This is where the
  hashes are.


```
b131d7e17dfdff73eb0340b4e9d3d6f3
8e92a45a6abed421488a5774ec3f4a4c 
ca05c73419475ade037f8df528849c91
ec9771e026e3ce55c468354abcfce4ee
c332f1814ff6d4f21dbb41b41149544d
```

  `albumdata_token.pmp`

  
  Here's we see
  the uid applied to create a token for
  the albums. Note that "star" and
  "screensaver" do not have uids.


```
]star
]screensaver
]album:b131d7e17dfdff73eb0340b4e9d3d6f3
]album:8e92a45a6abed421488a5774ec3f4a4c
]album:ca05c73419475ade037f8df528849c91
]album:ec9771e026e3ce55c468354abcfce4ee
]album:c332f1814ff6d4f21dbb41b41149544d 
```

  Now, if we look at the
  `lastalbumselected` value in the
  registry, we can pair it up to the
  hash since these files are all listed
  in the same order. If you exclude `star`
  and `screensaver` you can see that the
  `lastalbumselected` for me was `sam3`.

  
  You can even go one step further if
  you include albumdata_filename.pmp.
  This file also matches up to the other
  files, except I forgot to mention one
  thing. "root" is literally the root of
  the logical drive that picasa
  searched(in this case C:), so it is
  excluded from `albumdata_filename.pmp`.
  This file contains the path to where
  the images are stored.

  
  Other files to pay attention to:


```
bigthumbs.db 
thumbs2.db
thumbs.db
previews.db
```

  These all follow the good old
  `thumbs.db` structure and contain
  thumbnails of all of the images at
  various resolutions, since picasa can
  send files directly to photo
  processing businesses.

  
  One other thing that is of pretty
  vital importance in terms of proving
  that someone created an album and that
  the program didn't just index
  something.

  
  In the `Picasa2Albums` directory you'll
  see a file for each of the album(s)
  created by the user under the folder
  using the DBID as its name. Below are
  the contents of the album I created
  stored in a file named
  {c332f1814ff6d4f21dbb41b41149544d.pal.


```
'picasa2album>
'dbid>0164eaeacdd4046f5c1e44522fe44527
'albumid>c332f1814ff6d4f21dbb41b41149544d
'property name="uid" type="string" value="c332f1814ff6d4f21dbb41b41149544d">
'property name="category" type="num" value="0"> 
'property name="date" type="real64" value="39272.630035"
'property name="token" type="string" value="]album:c332f1814ff6d4f21dbb41b41149544d"
'property name="name" type="string" value="Sammy"
'files>
'filename>[C]\sam3\sam1.jpg
'filename>[C]\sam3\sam3.jpg
'filename>[C]\sam3\sam2.jpg
'filename>[C]\sam3\DSCF1890.JPG
'/files> 
'/property>
'/picasa2album>
```
