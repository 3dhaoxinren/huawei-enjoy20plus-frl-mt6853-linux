/* Mali-only GLES renderer: ION DMA-BUF -> DRM scanout, no CPU frame raster/readback. */
#include <EGL/egl.h>
#include <GLES2/gl2.h>
extern void *stderr;
extern int fprintf(void *,const char *,...);
extern int strcmp(const char *,const char *);
extern char *strstr(const char *,const char *);
extern int atoi(const char *);
extern void *calloc(unsigned long,unsigned long);
extern void free(void *);
extern long read(int,void *,unsigned long);
extern long write(int,const void *,unsigned long);
extern void *dlopen(const char *,int);
extern void *dlsym(void *,const char *);
extern int dlclose(void *);
typedef unsigned int U32;
#define GL_FUNCS(X) X(glGetString) X(glGetError) X(glGenTextures) X(glBindTexture) X(glTexParameteri) X(glTexImage2D) X(glTexSubImage2D) X(glPixelStorei) X(glGenFramebuffers) X(glBindFramebuffer) X(glFramebufferTexture2D) X(glCheckFramebufferStatus) X(glViewport) X(glClearColor) X(glClear) X(glEnable) X(glBlendFuncSeparate) X(glFinish) X(glDeleteTextures) X(glDeleteFramebuffers) X(glCreateShader) X(glShaderSource) X(glCompileShader) X(glGetShaderiv) X(glGetShaderInfoLog) X(glCreateProgram) X(glAttachShader) X(glBindAttribLocation) X(glLinkProgram) X(glGetProgramiv) X(glGetProgramInfoLog) X(glUseProgram) X(glGetUniformLocation) X(glUniform1i) X(glGenBuffers) X(glBindBuffer) X(glBufferData) X(glEnableVertexAttribArray) X(glVertexAttribPointer) X(glDrawArrays) X(glDeleteShader) X(glDeleteProgram) X(glDeleteBuffers)
#define EGL_FUNCS(X) X(eglGetDisplay) X(eglInitialize) X(eglGetError) X(eglChooseConfig) X(eglBindAPI) X(eglCreatePbufferSurface) X(eglCreateContext) X(eglMakeCurrent) X(eglDestroyContext) X(eglDestroySurface) X(eglTerminate)
#define DECL(n) static __typeof__(&n) p_##n;
GL_FUNCS(DECL) EGL_FUNCS(DECL)
#define LOAD(n) do {p_##n=(__typeof__(p_##n))dlsym(lib,#n);if(!p_##n){fprintf(stderr,"missing %s\n",#n);goto end;}}while(0);
#define NEED(x) do {if(!(x)){fprintf(stderr,"FAILED %s EGL=%x GL=%x\n",#x,p_eglGetError(),p_glGetError());goto end;}}while(0)
static int exact(int fd,void *data,unsigned long size,int output){
    char *p=data;while(size){long n=output?write(fd,p,size):read(fd,p,size);if(n<=0)return 0;p+=n;size-=(unsigned long)n;}return 1;
}
static GLuint shader(GLenum kind,const char *source){
    GLuint s=p_glCreateShader(kind);p_glShaderSource(s,1,&source,0);p_glCompileShader(s);
    GLint ok=0;p_glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
    if(!ok){char log[1024];p_glGetShaderInfoLog(s,1024,0,log);fprintf(stderr,"shader: %s\n",log);p_glDeleteShader(s);return 0;}return s;
}
int main(int argc,char **argv){
    if(argc!=7||strcmp(argv[1],"--scene"))return 64;
    int fds[2]={atoi(argv[2]),atoi(argv[3])},pitch=atoi(argv[4]),w=atoi(argv[5]),h=atoi(argv[6]);
    if(fds[0]<3||fds[1]<3||w!=1080||h!=2400||pitch<w*4||pitch>8192)return 64;
    int result=1,initialized=0;void *lib=0,*images[2]={0,0};
    EGLDisplay dpy=EGL_NO_DISPLAY;EGLSurface surf=EGL_NO_SURFACE;EGLContext ctx=EGL_NO_CONTEXT;
    EGLConfig config;EGLint major,minor,count;
    GLuint textures[2]={0,0},fbos[2]={0,0},atlas=0,vbo=0,program=0,vs=0,fs=0;
    void *(*create_image)(EGLDisplay,EGLContext,EGLenum,EGLClientBuffer,const EGLint*)=0;
    EGLBoolean (*destroy_image)(EGLDisplay,void*)=0;void (*target_image)(GLenum,void*)=0;
    float *vertices=0;unsigned char *pixels=0;
    lib=dlopen("/vendor/lib64/egl/libGLES_mali.so",2);if(!lib)goto end;
    GL_FUNCS(LOAD) EGL_FUNCS(LOAD)
    create_image=(__typeof__(create_image))dlsym(lib,"eglCreateImageKHR");
    destroy_image=(__typeof__(destroy_image))dlsym(lib,"eglDestroyImageKHR");
    target_image=(__typeof__(target_image))dlsym(lib,"glEGLImageTargetTexture2DOES");
    NEED(create_image&&destroy_image&&target_image);
    dpy=p_eglGetDisplay(EGL_DEFAULT_DISPLAY);NEED(dpy!=EGL_NO_DISPLAY);
    NEED(p_eglInitialize(dpy,&major,&minor));initialized=1;NEED(p_eglBindAPI(EGL_OPENGL_ES_API));
    const EGLint cfg[]={EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_ES2_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_ALPHA_SIZE,8,EGL_NONE};
    const EGLint pb[]={EGL_WIDTH,16,EGL_HEIGHT,16,EGL_NONE},ca[]={EGL_CONTEXT_CLIENT_VERSION,2,EGL_NONE};
    NEED(p_eglChooseConfig(dpy,cfg,&config,1,&count)&&count==1);
    surf=p_eglCreatePbufferSurface(dpy,config,pb);NEED(surf!=EGL_NO_SURFACE);
    ctx=p_eglCreateContext(dpy,config,EGL_NO_CONTEXT,ca);NEED(ctx!=EGL_NO_CONTEXT);
    NEED(p_eglMakeCurrent(dpy,surf,surf,ctx));
    const char *renderer=(const char*)p_glGetString(GL_RENDERER);
    NEED(renderer&&strstr(renderer,"Mali-G57")&&!strstr(renderer,"llvmpipe"));
    fprintf(stderr,"GL_RENDERER=%s\nGL_VERSION=%s\nPATH=ION-EGLImage-GLES-DRM no_glReadPixels=1\n",renderer,p_glGetString(GL_VERSION));
    for(int i=0;i<2;i++){
        const EGLint at[]={EGL_WIDTH,w,EGL_HEIGHT,h,0x3271,0x34325241,0x3272,fds[i],0x3273,0,0x3274,pitch,EGL_NONE};
        images[i]=create_image(dpy,EGL_NO_CONTEXT,0x3270,0,at);NEED(images[i]);
        p_glGenTextures(1,&textures[i]);p_glBindTexture(GL_TEXTURE_2D,textures[i]);
        p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
        target_image(GL_TEXTURE_2D,images[i]);NEED(p_glGetError()==GL_NO_ERROR);
        p_glGenFramebuffers(1,&fbos[i]);p_glBindFramebuffer(GL_FRAMEBUFFER,fbos[i]);
        p_glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,textures[i],0);
        NEED(p_glCheckFramebufferStatus(GL_FRAMEBUFFER)==GL_FRAMEBUFFER_COMPLETE);
    }
    vs=shader(GL_VERTEX_SHADER,"attribute vec2 pos;attribute vec2 uv;attribute vec4 color;varying vec2 tex;varying vec4 ink;void main(){gl_Position=vec4(pos.x/270.0-1.0,pos.y/600.0-1.0,0.0,1.0);tex=uv;ink=color;}");
    fs=shader(GL_FRAGMENT_SHADER,"precision mediump float;uniform sampler2D atlas;varying vec2 tex;varying vec4 ink;void main(){gl_FragColor=vec4(ink.rgb,ink.a*texture2D(atlas,tex).a);}");
    NEED(vs&&fs);program=p_glCreateProgram();p_glAttachShader(program,vs);p_glAttachShader(program,fs);
    p_glBindAttribLocation(program,0,"pos");p_glBindAttribLocation(program,1,"uv");p_glBindAttribLocation(program,2,"color");p_glLinkProgram(program);
    GLint ok=0;p_glGetProgramiv(program,GL_LINK_STATUS,&ok);NEED(ok);
    p_glUseProgram(program);p_glUniform1i(p_glGetUniformLocation(program,"atlas"),0);
    p_glGenTextures(1,&atlas);p_glBindTexture(GL_TEXTURE_2D,atlas);
    p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);p_glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    pixels=calloc(2048,2048);vertices=calloc(120000,8*sizeof(float));NEED(pixels&&vertices);
    pixels[0]=255;p_glPixelStorei(GL_UNPACK_ALIGNMENT,1);p_glTexImage2D(GL_TEXTURE_2D,0,GL_ALPHA,2048,2048,0,GL_ALPHA,GL_UNSIGNED_BYTE,pixels);
    p_glGenBuffers(1,&vbo);p_glBindBuffer(GL_ARRAY_BUFFER,vbo);
    for(int i=0;i<3;i++)p_glEnableVertexAttribArray((GLuint)i);
    p_glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,32,(void*)0);p_glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,32,(void*)8);p_glVertexAttribPointer(2,4,GL_FLOAT,GL_FALSE,32,(void*)16);
    p_glEnable(GL_BLEND);p_glBlendFuncSeparate(GL_SRC_ALPHA,GL_ONE_MINUS_SRC_ALPHA,GL_ONE,GL_ONE_MINUS_SRC_ALPHA);p_glViewport(0,0,w,h);
    U32 ready[2]={0x474c5246,0xffffffff};NEED(exact(1,ready,sizeof(ready),1));
    for(;;){
        U32 head[6];if(!exact(0,head,sizeof(head),0)){result=0;break;}
        NEED(head[0]==0x474c5246);
        if(head[1]==1){
            U32 x=head[2],y=head[3],tw=head[4],th=head[5];NEED(tw&&th&&tw<=512&&th<=512&&x<=2048-tw&&y<=2048-th);
            NEED(exact(0,pixels,tw*th,0));p_glBindTexture(GL_TEXTURE_2D,atlas);p_glTexSubImage2D(GL_TEXTURE_2D,0,(GLint)x,(GLint)y,(GLsizei)tw,(GLsizei)th,GL_ALPHA,GL_UNSIGNED_BYTE,pixels);
        }else if(head[1]==2){
            U32 index=head[2],n=head[3];NEED(index<2&&n<=120000&&n%3==0);
            NEED(exact(0,vertices,n*32,0));p_glBindFramebuffer(GL_FRAMEBUFFER,fbos[index]);
            p_glClearColor(244.0f/255,246.0f/255,248.0f/255,1);p_glClear(GL_COLOR_BUFFER_BIT);
            p_glBindTexture(GL_TEXTURE_2D,atlas);p_glBufferData(GL_ARRAY_BUFFER,n*32,vertices,GL_STREAM_DRAW);p_glDrawArrays(GL_TRIANGLES,0,(GLsizei)n);
            p_glFinish();NEED(p_glGetError()==GL_NO_ERROR);U32 ack[2]={0x474c5246,index};NEED(exact(1,ack,sizeof(ack),1));
        }else {NEED(0);}
    }
end:
    if(initialized&&ctx!=EGL_NO_CONTEXT){
        if(program)p_glDeleteProgram(program);if(vs)p_glDeleteShader(vs);if(fs)p_glDeleteShader(fs);
        if(vbo)p_glDeleteBuffers(1,&vbo);if(atlas)p_glDeleteTextures(1,&atlas);
        for(int i=0;i<2;i++){if(fbos[i])p_glDeleteFramebuffers(1,&fbos[i]);if(textures[i])p_glDeleteTextures(1,&textures[i]);if(images[i])destroy_image(dpy,images[i]);}
        p_eglMakeCurrent(dpy,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);p_eglDestroyContext(dpy,ctx);
    }
    if(initialized){if(surf!=EGL_NO_SURFACE)p_eglDestroySurface(dpy,surf);p_eglTerminate(dpy);}
    free(vertices);free(pixels);if(lib)dlclose(lib);fprintf(stderr,"EXIT=%d\n",result);return result;
}
