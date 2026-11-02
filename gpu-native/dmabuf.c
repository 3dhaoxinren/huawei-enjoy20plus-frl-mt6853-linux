/* Hardware-only Mali -> DRM PRIME allocation proof; no glReadPixels or CPU rendering. */
#include <EGL/egl.h>
#include <GLES2/gl2.h>
extern void *stderr;
extern int fprintf(void *, const char *, ...);
extern int fflush(void *);
extern int strcmp(const char *, const char *);
extern char *strstr(const char *, const char *);
extern int atoi(const char *);
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern const char *dlerror(void);
extern int dlclose(void *);
#define DECL(n) __typeof__(&n) p_##n = 0
#define LOAD(n) do { p_##n = (__typeof__(p_##n))dlsym(lib,#n); if(!p_##n){fprintf(stderr,"MISSING %s\n",#n);goto end;} } while(0)
#define NEED(x) do {if(!(x)){fprintf(stderr,"FAILED %s EGL=0x%x GL=0x%x\n",#x,p_eglGetError(),p_glGetError());goto end;}}while(0)

int main(int argc,char **argv){
 if(argc!=6||strcmp(argv[1],"--import-dmabuf")){fprintf(stderr,"usage: --import-dmabuf fd pitch width height\n");return 64;}
 int fd=atoi(argv[2]),pitch=atoi(argv[3]),width=atoi(argv[4]),height=atoi(argv[5]);
 if(fd<3||width<16||width>2400||height<16||height>2400||pitch<width*4)return 64;
 int result=1,initialized=0;
 void *lib=0,*image=0;
 EGLDisplay dpy=EGL_NO_DISPLAY;
 EGLSurface surface=EGL_NO_SURFACE;
 EGLContext context=EGL_NO_CONTEXT;
 EGLConfig config=0;EGLint major,minor,count;
 DECL(eglGetDisplay);DECL(eglInitialize);DECL(eglGetError);DECL(eglQueryString);
 DECL(eglChooseConfig);DECL(eglBindAPI);DECL(eglCreatePbufferSurface);DECL(eglCreateContext);
 DECL(eglMakeCurrent);DECL(eglDestroyContext);DECL(eglDestroySurface);DECL(eglTerminate);
 DECL(glGetString);DECL(glGetError);DECL(glGenTextures);DECL(glBindTexture);DECL(glTexParameteri);
 DECL(glGenFramebuffers);DECL(glBindFramebuffer);DECL(glFramebufferTexture2D);DECL(glCheckFramebufferStatus);
 DECL(glViewport);DECL(glClearColor);DECL(glClear);DECL(glEnable);DECL(glDisable);DECL(glScissor);DECL(glFinish);
 DECL(glDeleteTextures);DECL(glDeleteFramebuffers);
 void *(*create_image)(EGLDisplay,EGLContext,EGLenum,EGLClientBuffer,const EGLint*)=0;
 EGLBoolean (*destroy_image)(EGLDisplay,void*)=0;
 void (*target_image)(GLenum,void*)=0;
 GLuint texture=0,fbo=0;
 const EGLint cfg[]={EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_ES2_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_ALPHA_SIZE,8,EGL_NONE};
 const EGLint pb[]={EGL_WIDTH,16,EGL_HEIGHT,16,EGL_NONE};
 const EGLint ctx[]={EGL_CONTEXT_CLIENT_VERSION,2,EGL_NONE};
 lib=dlopen("/vendor/lib64/egl/libGLES_mali.so",2);
 if(!lib){fprintf(stderr,"DLOPEN %s\n",dlerror());goto end;}
 LOAD(eglGetDisplay);LOAD(eglInitialize);LOAD(eglGetError);LOAD(eglQueryString);
 LOAD(eglChooseConfig);LOAD(eglBindAPI);LOAD(eglCreatePbufferSurface);LOAD(eglCreateContext);
 LOAD(eglMakeCurrent);LOAD(eglDestroyContext);LOAD(eglDestroySurface);LOAD(eglTerminate);
 LOAD(glGetString);LOAD(glGetError);LOAD(glGenTextures);LOAD(glBindTexture);LOAD(glTexParameteri);
 LOAD(glGenFramebuffers);LOAD(glBindFramebuffer);LOAD(glFramebufferTexture2D);LOAD(glCheckFramebufferStatus);
 LOAD(glViewport);LOAD(glClearColor);LOAD(glClear);LOAD(glEnable);LOAD(glDisable);LOAD(glScissor);LOAD(glFinish);
 LOAD(glDeleteTextures);LOAD(glDeleteFramebuffers);
 create_image=(__typeof__(create_image))dlsym(lib,"eglCreateImageKHR");
 destroy_image=(__typeof__(destroy_image))dlsym(lib,"eglDestroyImageKHR");
 target_image=(__typeof__(target_image))dlsym(lib,"glEGLImageTargetTexture2DOES");
 NEED(create_image&&destroy_image&&target_image);
 dpy=p_eglGetDisplay(EGL_DEFAULT_DISPLAY);NEED(dpy!=EGL_NO_DISPLAY);
 NEED(p_eglInitialize(dpy,&major,&minor));initialized=1;
 fprintf(stderr,"EGL_VENDOR=%s\n",p_eglQueryString(dpy,EGL_VENDOR));
 NEED(p_eglBindAPI(EGL_OPENGL_ES_API));NEED(p_eglChooseConfig(dpy,cfg,&config,1,&count)&&count==1);
 surface=p_eglCreatePbufferSurface(dpy,config,pb);NEED(surface!=EGL_NO_SURFACE);
 context=p_eglCreateContext(dpy,config,EGL_NO_CONTEXT,ctx);NEED(context!=EGL_NO_CONTEXT);
 NEED(p_eglMakeCurrent(dpy,surface,surface,context));
 const char *renderer=(const char*)p_glGetString(GL_RENDERER);
 fprintf(stderr,"GL_RENDERER=%s\nGL_VERSION=%s\n",renderer,p_glGetString(GL_VERSION));
 NEED(renderer&&strstr(renderer,"Mali")&&!strstr(renderer,"llvmpipe"));
 const EGLint formats[]={0x34325241,0x34325258,0x34324241,0x34324258};
 for(int i=0;i<4;i++){
  const EGLint attrs[]={EGL_WIDTH,width,EGL_HEIGHT,height,0x3271,formats[i],0x3272,fd,0x3273,0,0x3274,pitch,EGL_NONE};
  image=create_image(dpy,EGL_NO_CONTEXT,0x3270,0,attrs);
  fprintf(stderr,"IMPORT fourcc=0x%x image=%p egl_error=0x%x\n",formats[i],image,p_eglGetError());fflush(0);
  if(image)break;
 }
 NEED(image);
 p_glGenTextures(1,&texture);p_glBindTexture(GL_TEXTURE_2D,texture);
 p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);
 p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
 target_image(GL_TEXTURE_2D,image);NEED(p_glGetError()==GL_NO_ERROR);
 p_glGenFramebuffers(1,&fbo);p_glBindFramebuffer(GL_FRAMEBUFFER,fbo);
 p_glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,texture,0);
 GLenum status=p_glCheckFramebufferStatus(GL_FRAMEBUFFER);
 fprintf(stderr,"FBO_STATUS=0x%x\n",status);NEED(status==GL_FRAMEBUFFER_COMPLETE);
 p_glViewport(0,0,width,height);p_glClearColor(1,0,0,1);p_glClear(GL_COLOR_BUFFER_BIT);
 p_glEnable(GL_SCISSOR_TEST);p_glScissor(0,height/2,width,height-height/2);
 p_glClearColor(0,1,0,1);p_glClear(GL_COLOR_BUFFER_BIT);p_glDisable(GL_SCISSOR_TEST);
 p_glFinish();NEED(p_glGetError()==GL_NO_ERROR);
 fprintf(stderr,"MALI_DIRECT_DMABUF_RENDER_PASS no_glReadPixels=1 cpu_render=0\n");
 result=0;
end:
 if(initialized){
  if(fbo)p_glDeleteFramebuffers(1,&fbo);
  if(texture)p_glDeleteTextures(1,&texture);
  if(image&&destroy_image)destroy_image(dpy,image);
  p_eglMakeCurrent(dpy,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
  if(context!=EGL_NO_CONTEXT)p_eglDestroyContext(dpy,context);
  if(surface!=EGL_NO_SURFACE)p_eglDestroySurface(dpy,surface);
  p_eglTerminate(dpy);
 }
 if(lib)dlclose(lib);
 fprintf(stderr,"EXIT=%d\n",result);fflush(0);return result;
}
